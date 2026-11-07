# 02. 成环回边：动态条件循环（While Loop）

当业务流程需要事先未知迭代次数的动态条件收敛循环（例如“生成结果 → 校验 → 失败则携带错误反馈重新生成”，或“从候选队列中逐个弹出节点尝试，一旦成功立即停止”）时，无需专用的循环关键字，直接在 `graph.edges` 中将分支出边指向上游已访问节点即可构成**成环回边（Back-Edge）**。

---

## 一、 成环回边的三项静态与运行时规则

### 1. 跨轮次状态变量必须在入环前完成初始化

循环体入口节点在第 1 轮从 `start` 进入时，循环体下游的更新节点尚未执行。因此，任何在循环入口被读取、并由循环体内部更新的跨轮状态变量（如重试计数 `attempts`、上轮错误反馈 `last_error`），必须在进入循环前已定值（静态常量初值可在 `graph.variables` 中通过 `TypeSpec` + `default` 声明；若需从输入结构体字段如 `request.items` 初始化动态工作队列，可在 `graph.variables` 中声明其 `TypeSpec`，并在入环前通过 [`builtin:assign`](../../reference/builtins/assign.md) 绑定至顶层变量）：

```yaml
graph:
  # 为跨轮次读取与更新的状态变量声明显式 TypeSpec 与首轮初始定值 default
  variables:
    attempts:
      type: integer
      default: 0
    last_error:
      type: string
      default: ""
  nodes:
    generate_candidate:
      # 第 1 轮读取 variables 初值；后续轮次读取回边传回的最新值
      input: [task_spec, last_error, attempts]
      output:
        candidate: { type: string }
        attempts: { type: integer }
      run: "..."
```

### 2. 环路必须存在可达 `end` 的退出分支

处于环路中的节点在拓扑上必须至少存在一条分支路径能够跳出环路并到达 `end`（或 `end.<branch>`），否则编译期静态检查器将判定为死环并拒绝通过：

```yaml
edges:
  - start -> generate_candidate
  - generate_candidate -> verify_candidate
  - verify_candidate.retry -> generate_candidate  # 成环回边：继续下一轮迭代
  - verify_candidate.pass -> end                  # 环路出口：跳出循环到达终点
```

### 3. 通过 `max_steps` 限制单图最大执行步数

在包含成环回边的 `graph` 上可配置 `max_steps`（未配置时默认上限为 `1000` 步），防止运行期因收敛条件一直未满足而无限执行：

```yaml
graph:
  max_steps: 20  # 当前图栈帧最多允许激活 20 个节点步数
  variables: ...
  nodes: ...
  edges: ...
```

---

## 二、 两类典型确定性循环模式

在成环回边循环中，通常结合运行时确定性内建算子推进队列消费、维护下标计数与更新跨轮状态（通用内建算子与数据操作的完整说明见下一节 [03. Builtins：确定性内建算子与数据操作](./03-builtin-operators.md) 及 [内建节点字典](../../reference/builtins/index.md)）。以下为最常用的两类确定性循环模式：

### 1. 动态队列消费与短路/折叠循环（`builtin:assign` + `builtin:array_shift` + `builtin:array_append`）

[`builtin:assign`](../../reference/builtins/assign.md) + [`builtin:array_shift`](../../reference/builtins/array-shift.md) + [`builtin:array_append`](../../reference/builtins/array-append.md) 配合 CFG 成环回边不仅支持**短路搜索（Short-Circuit）**，同时也是表达**跨元素有状态串行折叠（Stateful Sequential Fold）**的标准方式（支持分支按需更新状态变量或跳过无需更新的分支，详见 [配方 01：有状态串行折叠](../../cookbook/02-collection-and-concurrency/01-stateful-sequential-fold.md)）。

当待消费的列表位于输入结构体内部（如 `batch_request.urls`）时，先在 `graph.variables` 中声明顶层队列变量 `pending_urls` 与元素变量 `current_url` 的 `TypeSpec`，在入环前通过 `builtin:assign` 将 `batch_request.urls` 绑定至 `pending_urls`。在循环头 `builtin:array_shift` 中，将 `with.output.rest` 与 `with.input.source` 绑定为同一个顶层变量 `pending_urls`：
* **`.some` 分支（队列非空）**：弹出首元素定值给 `item`（`current_url`），并将首元素之后的新切片副本重新绑定至队列变量 `rest`（`pending_urls`）；结合 `builtin:array_append` 可在每轮失败时将错误日志追加至历史列表并经由回边进入下一轮；
* **`.empty` 分支（队列已空）**：直接跳出循环，同时将 `with.output.item` 绑定的变量（`current_url`）重置为**未定值（Unassigned）**状态，防止环外下游节点误读最后一轮残留的旧元素。

```yaml
graph:
  variables:
    # 1. 初始化用于累积失败记录的空列表（TypeSpec + default: []）
    error_logs:
      type: array
      items: { type: string }
      default: []
    # 2. 显式声明仅由 builtin:assign / builtin:array_shift 写入的队列与当前元素变量类型
    pending_urls:
      type: array
      items: { type: string }
    current_url:
      type: string

  nodes:
    # 0. 入环前将结构体字段 batch_request.urls 赋值给顶层队列变量 pending_urls
    init_url_queue:
      use: builtin:assign
      with:
        input:
          source: batch_request.urls
        output:
          value: pending_urls

    # 1. 从队列头部弹出首元素 current_url，并将剩余新切片副本重新绑定至 pending_urls
    pop_url:
      use: builtin:array_shift
      with:
        input:
          source: pending_urls
        output:
          item: current_url
          rest: pending_urls

    # 2. 尝试请求当前弹出的 current_url
    try_fetch:
      input: [current_url]
      output:
        page_body: { type: string }
        fail_msg: { type: string }
      branches:
        ok:
          description: "请求成功，短路退出循环"
          output: [page_body]
        failed:
          description: "请求失败，记录错误并继续尝试下一项"
          output: [fail_msg]
      run: "请求 variables.current_url；成功写入 output.page_body 并选 ok，失败写入 output.fail_msg 并选 failed。"

    # 3. 将单次失败原因 fail_msg 追加至 error_logs 列表末尾
    append_error:
      use: builtin:array_append
      with:
        input:
          source: error_logs
          item: fail_msg
        output:
          result: error_logs

  edges:
    - start -> init_url_queue
    - init_url_queue -> pop_url
    - pop_url.some -> try_fetch
    - try_fetch.ok -> end.success        # 命中即停：无需遍历剩余元素，直接退出
    - try_fetch.failed -> append_error
    - append_error -> pop_url            # 成环回边：回到 pop_url 弹出下一个候选元素
    - pop_url.empty -> end.exhausted     # 队列已空：跳出循环并重置 current_url 定值状态
```

### 2. 非破坏性下标计数循环（`builtin:length` + `builtin:compare` + `builtin:array_get` + `builtin:math`）

当需要在不消费原列表的前提下按索引遍历或抽样访问列表（如 `while (idx < n)`），可组合使用 [`builtin:length`](../../reference/builtins/length.md)、[`builtin:compare`](../../reference/builtins/compare.md)、[`builtin:array_get`](../../reference/builtins/array-get.md) 与 [`builtin:math`](../../reference/builtins/math.md)，无需调用任何外部 `run` 执行器即可在图内零开销维护下标与数值累加：

```yaml
graph:
  variables:
    idx:
      type: integer
      default: 0
    total_len:
      type: integer
    current_score:
      type: number
    accumulated_score:
      type: number
      default: 0.0

  nodes:
    # 1. 入环前非破坏性地测量 scores 列表长度
    get_total_len:
      use: builtin:length
      with:
        input:
          source: scores
        output:
          result: total_len

    # 2. 循环头条件判断：idx < total_len（暴露 check_bound.true 与 check_bound.false）
    check_bound:
      use: builtin:compare
      with:
        input:
          op: { value: "lt" }
          left: idx
          right: total_len

    # 3. 按当前下标 idx 非破坏性读取列表元素（传入 default: 0.0 收敛为无分支单出边）
    read_score_at_idx:
      use: builtin:array_get
      with:
        input:
          source: scores
          index: idx
          default: 0.0
        output:
          value: current_score

    # 4. 累加总分：accumulated_score = accumulated_score + current_score
    add_to_total:
      use: builtin:math
      with:
        input:
          op: { value: "add" }
          left: accumulated_score
          right: current_score
        output:
          result: accumulated_score

    # 5. 下标自增：idx = idx + 1
    increment_idx:
      use: builtin:math
      with:
        input:
          op: { value: "add" }
          left: idx
          right: 1
        output:
          result: idx

  edges:
    - start -> get_total_len
    - get_total_len -> check_bound
    - check_bound.true -> read_score_at_idx
    - read_score_at_idx -> add_to_total
    - add_to_total -> increment_idx
    - increment_idx -> check_bound       # 成环回边：回到循环头检查下一轮下标
    - check_bound.false -> end           # 遍历完毕：跳出循环
```

---

## 三、 完整示例：生成-校验-修正循环

以下完整工作流通过 `graph.variables` 初始化跨轮状态，结合 `builtin:math` 递增尝试次数，并在校验未通过时经由成环回边跳回生成节点，直至校验通过后退出：

```yaml
# 1. 根节点输入与输出契约
input:
  task_spec:
    type: string
    description: "代码生成需求说明"
output:
  candidate:
    type: string
    description: "通过校验的最终代码"
  attempts:
    type: integer
    description: "累计生成尝试次数"

graph:
  # 2. 设置单图最大步数上限，防止动态循环不收敛
  max_steps: 20

  # 3. 初始化跨轮次状态变量：首轮进入循环时读取此初值
  variables:
    attempts:
      type: integer
      default: 0
    last_error:
      type: string
      default: ""

  nodes:
    # 循环体步骤 1：使用 builtin:math 确定性地将 attempts 自增 1
    increment_attempts:
      use: builtin:math
      with:
        input:
          op: { value: "add" }
          left: attempts
          right: 1
        output:
          result: attempts

    # 循环体步骤 2：读取需求、上轮错误信息与当前计数，产出候选结果
    generate_candidate:
      input: [task_spec, last_error, attempts]
      output:
        candidate: { type: string }
      run: "根据需求 variables.task_spec 与上次报错 variables.last_error（第 variables.attempts 次尝试）生成代码写入 output.candidate。"

    # 循环体步骤 3：校验候选代码
    # - pass 分支声明 output: []，保持原有状态直接退出
    # - retry 分支声明 output: [last_error]，更新错误反馈供下一轮读取
    verify_candidate:
      input: [candidate, attempts]
      output:
        last_error: { type: string }
      branches:
        pass:
          description: "校验通过，结束循环"
          output: []
        retry:
          description: "校验未通过，携带 last_error 回边重试"
          output: [last_error]
      run: "检验 variables.candidate；通过则选择 pass，否则将错误原因写入 output.last_error 并选择 retry。"

  # 4. 控制流连边：包含回边与退环出口
  edges:
    - start -> increment_attempts
    - increment_attempts -> generate_candidate
    - generate_candidate -> verify_candidate
    # 成环回边：将更新后的 last_error 带回 increment_attempts 进入下一轮
    - verify_candidate.retry -> increment_attempts
    # 退环出口：candidate 与 attempts 均已定值，正常结束工作流
    - verify_candidate.pass -> end
```

> 包含最大重试次数早退分支及队列回退链的生产级模板，可参阅 Cookbook：
> * [生成-校验-修正闭环](../../cookbook/01-loops-and-queues/01-generate-verify-refine-loop.md)
> * [候选节点短路早退回退链](../../cookbook/01-loops-and-queues/02-short-circuit-fallback-chain.md)

---

下一节：[03. Builtins：确定性内建算子与数据操作](./03-builtin-operators.md)。
