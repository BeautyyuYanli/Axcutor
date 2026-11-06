# 工作流异常处理与容错模型规范

本文定义工作流 DSL 的运行时异常分类、栈帧回滚与冒泡生命周期、节点级超时与原地重试（`timeout_ms` / `retry`）、异常边界捕获路由（`catch`）以及图级主动异常终态（`fail`）。

关于工作流整体架构、类型系统与静态定值分析，请参见 `DSL.md`；关于运行时内建算子请参见 `builtins.md`；关于循环与集合遍历的组合模式请参见 `while_and_iter.md` 与 `while_plus_iter.md`。

---

## 一、 核心设计原则与三级错误分层

在结构化并发控制流图（Structured CFG）与双层栈帧（`Function Frame` / `Block Frame`）架构下，工作流的错误与异常按**状态所有权（Commit vs. Rollback）**与**控制流传播方式（Local Edge vs. Stack Unwinding）**严格划分为三个层级：

| 层级 | 分类 | 核心语义与状态行为 | 控制流机制 |
| :--- | :--- | :--- | :--- |
| **第一级** | **显式业务分支**<br>*(Expected Business Branches)* | 节点正常完成，产出的字段通过 `output` 的 `TypeSpec` 校验并**原子提交（Commit）**回所属图的 `variables`。 | 通过节点声明的 `branches.<choice>` 与 `graph.edges` 中的显式单跳出边（或 `for_each` 内部消费）路由。 |
| **第二级** | **可恢复运行时异常**<br>*(Recoverable Runtime Exceptions)* | 节点或子图执行中断，当前节点/子图栈帧内未提交的中间状态**全部原子回滚（Rollback）**，绝不污染外层图的 `variables`。 | 优先由节点自身的 `retry` 原地重试；若未配置重试或重试耗尽，则由节点边界的 `catch` 物化为控制分支（`output: []`）；若未被 `catch` 捕获，则自动跳过当前图后续节点并沿调用栈向上冒泡展开（Unwind）。 |
| **第三级** | **不可恢复引擎致命终止**<br>*(Unrecoverable Engine Panics)* | 触及全局运行红线或宿主取消信号，不再允许调度任何恢复或补偿节点。 | **严禁被 `retry` 或 `catch` 拦截**，立即取消所有在途并发分支、展开全部栈帧并终止整个工作流。 |

---

## 二、 内置异常分类体系（Exception Taxonomy）

### 1. 四类可恢复运行时异常（可用于 `retry` 与 `catch`）

运行时引擎内置以下 4 种标准可恢复异常选择器（Exception Selector）：

| 异常选择器 | 触发边界与语义 | 允许产生的节点形态 |
| :--- | :--- | :--- |
| **`execution`** | 1. `run` 节点执行器发生运行期故障（如外部 API 请求失败、Python 沙箱抛出未捕获异常或非零退出、Agent/LLM 调用失败）；<br>2. 内联 `graph` 或 `use` 子工作流内部控制流显式到达 `-> fail` 终态。 | `run`、`graph`、`use`（调用外部工作流） |
| **`timeout`** | 节点单次执行（含 `run` 动作、整个内联 `graph` 子图或 `use` 子工作流调用）耗时超过该节点声明的 `timeout_ms` 阈值。 | 声明了 `timeout_ms`（或内部含 `timeout_ms`）的 `run`、`graph`、`use` |
| **`contract`** | 节点执行器返回的产出数据结构未通过 `output` 声明的 JSON Schema（`TypeSpec`）校验，或返回了未在 `branches` 中声明的非法分支名。 | `run`、`graph`、`use` |
| **`max_steps`** | 内联 `graph` 或 `use` 调用的子工作流在包含环路的图调度中，累计执行步数超出其 `graph.max_steps` 上限（触发局部 `MaxStepsExceededError`）。 | 内联 `graph`、`use`（调用外部工作流） |

> **注**：确定性内建算子（`use: builtin:<op>`）在编译期已完成全部静态类型与契约检查，运行期零 I/O 且必定在常数时间内成功完成，因此 `builtin:*` 节点不产生上述可恢复运行时异常，也禁止在 `builtin:*` 节点上声明 `timeout_ms`、`retry` 或 `catch`。

### 2. 三类不可恢复引擎致命终止（不可被 `retry` 或 `catch` 拦截）

以下引擎级事件属于不可恢复致命终止（Panic），发生时无条件穿透所有栈帧：
1. **`StackOverflowError`**：调用链深度（`use` 与内联 `graph` 嵌套层数）超出运行时 `max_call_depth` 阈值（默认 `64`）。
2. **根工作流 `MaxStepsExceededError`**：最顶层根图（Root Graph）自身的累计步数超出其 `max_steps`（已无外层父图栈帧可接管）。
3. **`CancelledError`**：宿主环境主动取消工作流运行，或同属一个并发组 `[worker_a, worker_b]` / 并发 `for_each` 的兄弟分支发生未捕获异常触发 Fail-Fast 协同取消。

---

## 三、 节点级超时与原地重试（`timeout_ms` & `retry`）

在图内任意非内建节点（`run`、内联 `graph`、调用外部工作流的 `use`）以及顶层为 `run` 的根节点上，支持可选声明 `timeout_ms` 与 `retry`，在异常逃逸出当前节点前基于**只读输入快照**完成原地恢复。

### 1. 语法契约与字段定义

```yaml
nodes:
  fetch_remote_data:
    timeout_ms: 5000                  # 可选：单次尝试的最大允许耗时（正整数，单位：毫秒）
    retry:                            # 可选：节点内原地重试策略
      max_attempts: 3                 #   必填：最大尝试总次数（含首次执行，必须为 >= 2 的整数）
      backoff_ms: 1000                #   可选：两次尝试之间的退避等待毫秒数（非负整数，默认 0）
      on: [execution, timeout]        #   可选：允许触发重试的异常选择器列表（去重非空子集，默认 [execution, timeout]）
    input: [endpoint_url]
    output:
      payload: { type: string }
    run: "请求接口 variables.endpoint_url 并返回响应报文写入 output.payload。"
```

### 2. 生命周期与重试语义

1. **单次尝试计时边界（`timeout_ms`）**：
   * 当节点同时声明 `timeout_ms` 与 `retry` 时，`timeout_ms` 约束的是**每一次独立尝试（Attempt）**的耗时上限（而非所有重试累计耗时）。单次尝试超时即中断当次执行并产生 `timeout` 异常。
2. **快照重放与脏状态丢弃**：
   * 节点首次激活时，运行时锁定该节点 `input`（或 `with.input`）的只读快照。
   * 当第 $k$ 次尝试（$1 \le k < \text{max\_attempts}$）抛出可恢复异常 $E$，且 $E \in \text{retry.on}$ 时，运行时立即丢弃本次尝试内部产生的一切半成品状态（若该节点为内联 `graph` 或 `use`，则销毁其整个子栈帧），等待 `backoff_ms` 后使用同一份初始只读快照启动第 $k+1$ 次尝试。
   * 若第 $k$ 次尝试正常完成并通过 `output` Schema 校验，节点直接提交产出并沿正常出边前进，后续不再触发 `catch`。
3. **重试耗尽与不匹配直通**：
   * 若抛出的异常 $E \notin \text{retry.on}$（例如默认配置下发生 `contract` 校验违约），或第 $\text{max\_attempts}$ 次尝试依然失败，重试控制器终止，将该异常移交当前节点的 `catch` 边界拦截器处理（若未声明 `catch` 则直接向上冒泡）。
4. **与 `for_each` 组合时的作用粒度**：
   * 当节点同时挂载 `for_each` 与 `timeout_ms` / `retry` 时，计时器与重试计数器严格作用于**单次元素迭代粒度**——仅对发生故障的那个具体元素原地重试，已成功的其他元素无需重跑。

---

## 四、 节点边界异常捕获（`catch`：异常物化为控制分支）

当异常未被 `retry` 消化时，节点可通过声明 `catch` 将正在向上冒泡的运行时异常拦截，并在当前节点边界将其**物化（Reify）为所属图的一条或多条控制分支**。

### 1. 语法契约

`catch` 为映射结构 `{ <catch_branch_name>: [<selector>, ...] | "*" }`，其中每个键 `<catch_branch_name>` 定义一条异常出口分支，值定义该分支拦截的异常集合：

```yaml
nodes:
  # 形态 A：单分支统一兜底（使用 "*" 通配拦截所有可恢复异常，或指定特定异常列表）
  query_primary_source:
    timeout_ms: 3000
    retry:
      max_attempts: 2
      on: [execution, timeout]
    catch:
      fallback: "*"                   # 拦截全部可恢复异常 (execution / timeout / contract / max_steps)，暴露 query_primary_source.fallback 出边
    input: [query_key]
    output:
      record: { type: string }
    run: "从主数据源查询 variables.query_key，写入 output.record。"

  # 形态 B：多分支按异常类型精准分流（未列出的异常自动继续向上冒泡）
  autonomous_research_subgraph:
    input: [topic]
    output: [report]
    catch:
      # 1. 子图循环超出 max_steps 或整体超时 -> 转入 .bounded_stop 分支做阶段性总结
      bounded_stop: [max_steps, timeout]
      # 2. 子图内部节点执行报错 -> 转入 .exec_failed 分支走备用检索方案
      exec_failed: [execution]
      # 注：此处未拦截 contract 异常；若发生 contract 违约将直接穿透该节点向上层栈帧冒泡
    graph:
      max_steps: 20
      nodes: ...
      edges: ...
```

### 2. 静态校验铁律

1. **选择器合法性与分支间互斥**：
   * 每个 `<catch_branch_name>` 的值必须是非空数组（元素取自 `[execution, timeout, contract, max_steps]` 且不重复），或通配符 `"*"`（等价于 `["*"]`，代表捕获当前节点其余所有未被显式列出的可恢复异常）。
   * 同一节点的 `catch` 映射中，不同分支声明的异常选择器集合**必须两两互斥（无交集）**，且通配符 `"*"` 最多只能在一个分支中出现，否则编译期直接报错。
   * 在普通 `run` 节点上，由于其自身不维护图步数计数器，禁止在 `run` 节点的 `retry.on` 或 `catch` 显式列表中声明 `max_steps`（通配符 `"*"` 自动忽略不适用项）。
2. **分支命名无冲突**：
   * `catch` 中声明的所有 `<catch_branch_name>` **不可与该节点 `branches` 中已声明的正常业务分支同名**，也不可使用保留字 `start`、`end`、`fail`。
3. **拓扑出边与并发组约束（与 `branches` 完全同构）**：
   * **当节点未挂载 `for_each` 时**：
     * 若节点原本无正常 `branches`，声明 `catch` 后，该节点在 `graph.edges` 中除了必须有唯一一条正常默认出边 `node -> normal_target` 外，还**必须为 `catch` 中的每一个 `<catch_branch_name>` 声明且仅声明一条异常出边 `node.<catch_branch_name> -> error_target`**。
     * 若节点原本已有正常 `branches`，则 `graph.edges` 必须同时穷尽该节点所有的正常分支 `node.<choice>` 与所有的异常分支 `node.<catch_branch_name>`。
   * **当节点挂载了 `for_each` 时**：
     * `catch` 声明的分支与正常 `branches` 一样，仅在**单次迭代局部帧粒度**生效并由 `for_each` 内部屏障消费（外层 `graph.edges` 依然只能声明唯一无分支出边 `node -> target`）。
   * **结构化并发组约束**：出现在并发组 `[worker_a, worker_b]` 内部的直接成员节点（若未挂载 `for_each`），**禁止声明 `catch`**（正如禁止直接声明对外 `branches` 一样；若单个并发分支内部需要捕获异常并恢复，须将该分支封装为 SESE 内联 `graph` 节点，在子图内部消化异常并统一汇合至子图唯一的 `end`）。

### 3. 状态回滚与分支敏感静态定值规则（Definite Assignment）

在分支敏感静态定值检查（参见 `DSL.md` 第四.2 节）与运行时状态提交中，**每一个 `catch` 分支在语义上严格等价于一个声明了 `output: []`（不产出任何变量）的分支**：

1. **运行时原子回滚（Zero Dirty Write）**：
   * 当节点（或内联 `graph` / `use`）因异常进入 `catch.<catch_branch_name>` 时，该节点在执行期间产生的一切未提交产出或子图局部变量修改**全部丢弃**，不向所属外层图的 `variables` 写入任何字段（外层图中原本已存在的同名变量保持进入该节点前的旧值不变）。
2. **静态定值传播方程**：
   * 设节点 $N$ 的入口已定值变量集合为 $Def_{in}(N)$：
     * 沿正常默认出边 $e_{ok} = (N \to \text{target})$：$Def_{out}(e_{ok}) = Def_{in}(N) \cup OutVars(N)$；
     * 沿正常业务分支出边 $e_{c} = (N.\text{choice} \to \text{target})$：$Def_{out}(e_{c}) = Def_{in}(N) \cup OutVars(N, \text{choice})$；
     * **沿异常捕获出边 $e_{err} = (N.\text{<catch\_branch>} \to \text{target})$**：由于该分支不产出任何节点输出变量（$OutVars(N, \text{<catch\_branch>}) = \emptyset$），其出边定值集恒为：
       $$Def_{out}(e_{err}) = Def_{in}(N)$$
   * **编译期安全拦截**：若节点 $N$ 负责首次初始化变量 `X`，且 `N.<catch_branch>` 未经过为 `X` 赋兜底值的补偿节点就直接汇合到下游读取 `X` 的节点 $M$，则因 $X \notin Def_{out}(e_{err})$ 导致 $X \notin Def_{in}(M)$，静态检查器将在编译期 100% 报错拦截。

---

## 五、 图级异常回滚终态（`fail`）与跨栈帧展开（Stack Unwinding）

### 1. 图级主动异常终态：`source -> fail` / `source.choice -> fail`

在 `graph.edges` 中，与正常提交出口 `end`（及 `end.<branch>`）对称，提供图级异常回滚终态端点 **`fail`**：

| 终态连边形式 | 栈帧生命周期与状态行为 | 静态定值与拓扑要求 |
| :--- | :--- | :--- |
| `source -> end`<br>`source.choice -> end.<branch>` | **正常完成并提交（Commit）**：结束当前图栈帧，按出口关联的宿主 `output` 字段集提取当前图 `variables` 并提交给调用方。 | 到达该 `end` 分支的每条路径上，关联的宿主 `output` 变量必须全部已定值。 |
| `source -> fail`<br>`source.choice -> fail` | **异常回滚并抛出（Rollback & Throw）**：立即中止当前图栈帧，**丢弃当前图对宿主 `output` 的一切未提交状态**，向父调用栈帧抛出一个 `execution` 异常。 | 1. 到达 `fail` 的路径上**不要求**宿主 `output` 变量已定值；<br>2. 图内依然必须至少存在一条从 `start` 能够到达 `end`（或 `end.<branch>`）的拓扑路径（禁止全图只有 `fail` 没有 `end`）。 |

* **典型用途**：
  1. **将业务错误分支升级为事务回滚异常**：在内联 `graph` 多步事务中，若某一步前置校验走入 `.invalid` 分支，可直接连边 `check_node.invalid -> fail`，立即触发整个子图栈帧回滚并激活外层节点的 `catch`。
  2. **捕获后重抛（Re-throw）**：在 `catch` 分支中进行必要的日志记录或外部状态清理后，通过 `cleanup_node -> fail` 继续将异常向上层栈帧抛出。

### 2. 未捕获异常的跨栈帧冒泡展开（Uncaught Unwinding）

当图内某个节点抛出可恢复异常 $E$，且该节点未声明 `catch`（或 $E$ 未命中该节点任何 `catch` 分支）时，触发**栈展开（Stack Unwinding）**：
1. **并发组与批处理协同取消（Fail-Fast Cancellation）**：
   * 若出错节点正处于结构化并发组 `[worker_a, worker_b]` 中，或处于 `for_each` 并发迭代中（且该迭代未在内部 `catch` 消化该异常），运行时立即向同组所有尚未完成的并发兄弟任务发送 `CancelledError` 并等待其终止，丢弃该并发组或 `for_each` 已暂存的全部部分输出。
2. **当前图栈帧回滚中断**：
   * 当前所属图立即停止后续边调度，丢弃当前图栈帧内的一切局部变量演进。
3. **向父栈帧传播**：
   * 若当前图是**内联子图（`Block Frame`）**或**被 `use` 调用的子工作流（`Function Frame`）**：异常 $E$（保留其原始异常类型 `execution` / `timeout` / `contract` / `max_steps`）直接传递到外层包裹该子图的 `graph` 节点或 `use` 节点边界，由外层节点的 `retry` 与 `catch` 按相同规则处理。
   * 若当前图已经是**最顶层根工作流（Root Graph）**：整个工作流运行以未捕获异常状态终止，不产出任何顶层 `output`。

---

## 六、 与 SESE 内联 `graph` 及 `for_each` 的正交组合模式

无需引入任何额外语法，`timeout_ms`、`retry`、`catch` 与 `fail` 直接与现有的 SESE 内联 `graph` 和 `for_each` 组合成完备的工程容错模式。

### 模式一：SESE 内联 `graph` 作为多步原子事务 `try-catch` 容器

当多步操作（如串联多个 API 调用、Python 沙箱计算与子工作流）需要共享同一个超时/重试/异常恢复边界时，将其包裹在内联 `graph` 节点中。子图内部只需编写纯粹的正常连边；任何一步发生异常，子图局部状态自动原子回滚，并在外层内联 `graph` 节点的 `catch` 分支统一恢复：

```yaml
input:
  order_id: { type: string }
output:
  settlement_id: { type: string }
  status_note: { type: string }
branches:
  settled:
    description: "清算完成"
    output: [settlement_id, status_note]
  deferred:
    description: "外部依赖故障或超时，已转入延迟队列"
    output: [status_note]

graph:
  nodes:
    # SESE 内联子图作为事务块：内部 3 个步骤共享异常回滚边界
    settlement_transaction:
      timeout_ms: 15000               # 整个 3 步子图事务的总超时上限为 15 秒
      input: [order_id]
      output: [settlement_id, status_note]
      catch:
        # 无论子图内哪一步发生超时、执行报错或契约违约，均回滚子图并转入 .tx_failed 出边
        tx_failed: [execution, timeout, contract]
      graph:
        nodes:
          fetch_order:
            timeout_ms: 3000
            retry:
              max_attempts: 3
              backoff_ms: 500
              on: [execution, timeout]
            input: [order_id]
            output:
              order_amount: { type: number }
            run: "调用订单 API 获取 variables.order_id 的金额，写入 output.order_amount。"

          verify_amount:
            input: [order_amount]
            branches:
              valid: "金额合法"
              invalid: "金额为负数或异常"
            run: "在 Python 沙箱中校验 variables.order_amount 是否合法。"

          submit_clearing:
            timeout_ms: 5000
            input: [order_id, order_amount]
            output:
              settlement_id: { type: string }
              status_note: { type: string }
            run: "调用清算接口提交 variables.order_id 与 variables.order_amount，写入 output.settlement_id 与 output.status_note。"

        edges:
          - start -> fetch_order
          - fetch_order -> verify_amount
          # 若校验不合法，通过 -> fail 主动触发子图回滚并向父节点抛出 execution 异常
          - verify_amount.invalid -> fail
          - verify_amount.valid -> submit_clearing
          - submit_clearing -> end

    # 异常补偿节点：在 .tx_failed 分支上定值 status_note
    enqueue_retry_job:
      input: [order_id]
      output:
        status_note: { type: string }
      run: "将订单 variables.order_id 加入延迟重试队列，并将排队说明写入 output.status_note。"

  edges:
    - start -> settlement_transaction
    # 正常完成：settlement_id 与 status_note 均已定值
    - settlement_transaction -> end.settled
    # 事务异常回滚：settlement_id 未定值，经 enqueue_retry_job 定值 status_note 后从 end.deferred 退出
    - settlement_transaction.tx_failed -> enqueue_retry_job
    - enqueue_retry_job -> end.deferred
```

---

### 模式二：`for_each` 批处理中的元素级异常隔离与多路死信分流

当 `for_each` 节点同时声明 `catch` 时，`catch` 的各个异常分支在**单次元素迭代粒度**被 `for_each` 屏障直接消费，并与 `filter`、`collect`、`state` 三类通道无缝协同：
1. **`for_each.filter`（死信与待重跑原元素零拷贝分流）**：可将走入不同 `catch` 分支的原始输入元素 `item`（或有序映射键值对）零拷贝归档到不同的外层集合变量中（如将 `timeout` 的元素归入 `retry_later_items`，将 `execution` 的元素归入 `dead_letter_items`）。
2. **`for_each.collect`（无 `null` 收集成功产出）**：由于进入 `catch` 分支的迭代不产出任何业务 `output` 字段（等价于 `output: []`），`collect` 自动跳过发生异常的元素，仅收集正常完成元素的非空产出。
3. **`for_each.state`（串行累积折叠状态保护）**：在 `concurrency: 1` 的有状态折叠中，进入 `catch` 分支的迭代不会覆盖累积状态变量，自动保留上一轮成功迭代的累积值继续后续元素的遍历。
4. **未捕获异常触发整批熔断**：若某次迭代抛出的异常未被 `catch` 覆盖（例如 `catch` 仅拦截了 `[execution, timeout]`，但运行期发生了 `contract` 契约损坏），则立即触发 `for_each` 的 **Fail-Fast 整批取消**并向上栈展开。

```yaml
input:
  raw_tasks:
    type: array
    items:
      type: object
      properties:
        task_id: { type: string }
        script: { type: string }
      required: [task_id, script]
output:
  completed_outputs:
    type: array
    items: { type: string }
  timed_out_tasks:
    type: array
    items:
      type: object
      properties:
        task_id: { type: string }
        script: { type: string }
      required: [task_id, script]
  crashed_tasks:
    type: array
    items:
      type: object
      properties:
        task_id: { type: string }
        script: { type: string }
      required: [task_id, script]

graph:
  nodes:
    batch_execute_scripts:
      for_each:
        source: raw_tasks
        item: current_task
        concurrency: 4
        # 1. 按 catch 分支名，零拷贝将原始 current_task 结构体分流至对应的隔离队列
        filter:
          on_timeout: timed_out_tasks
          on_crash: crashed_tasks
        # 2. 自动仅从正常完成的迭代中收集 stdout_text（异常迭代因 output: [] 自动跳过）
        collect:
          stdout_text: completed_outputs
      timeout_ms: 5000
      retry:
        max_attempts: 2
        on: [timeout]                 # 仅对单元素超时原地重试 1 次
      catch:
        on_timeout: [timeout]         # 重试后仍超时 -> 走 on_timeout 分支归入 timed_out_tasks
        on_crash: [execution]         # 沙箱脚本报错 -> 走 on_crash 分支归入 crashed_tasks
        # 注：contract 违约未在此捕获，一旦发生立即 Fail-Fast 熔断取消整批任务
      input: [current_task]
      output:
        stdout_text: { type: string }
      run: "在 Python 沙箱中执行任务 variables.current_task.task_id 的脚本 variables.current_task.script，将标准输出写入 output.stdout_text。"

  edges:
    - start -> batch_execute_scripts
    - batch_execute_scripts -> end
```
