# 02. 成环回边：动态条件循环（While Loop）

当业务流程需要事先未知迭代次数的动态条件收敛循环（例如“生成结果 → 校验 → 失败则携带错误反馈重新生成”，或“从候选队列中逐个弹出节点尝试，一旦成功立即停止”）时，无需专用的循环关键字，直接在 `graph.edges` 中将分支出边指向上游已访问节点即可构成**成环回边（Back-Edge）**。

---

## 一、 成环回边的三项静态与运行时规则

### 1. 跨轮次状态变量必须在入环前完成初始化

循环体入口节点在第 1 轮从 `start` 进入时，循环体下游的更新节点尚未执行。因此，任何在循环入口被读取、并由循环体内部更新的跨轮状态变量（如重试计数 `attempts`、上轮错误反馈 `last_error`），必须在进入循环前已定值（静态常量初值可在 `graph.variables` 中声明；若需从输入结构体字段如 `request.items` 初始化动态工作队列，可在入环前通过 [`builtin:assign`](../../reference/builtins/assign.md) 绑定至顶层变量）：

```yaml
graph:
  # 为跨轮次读取与更新的状态变量提供首轮初始定值
  variables:
    attempts: 0
    last_error: ""
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

## 二、 循环与流水线中的确定性数据与集合操作算子

在成环回边循环或串行流水线中，若需初始化工作队列、从队列头部逐个弹出元素执行短路遍历、向列表追加记录、读写/删除有序映射键值对，或不可变地组装/更新强类型结构体（`TypedDict`），可调用以下运行时内建算子（完整参数规范见 [内建节点字典](../../reference/builtins/index.md)）：

| 内建算子 | 核心用途 | 控制分支与输出契约 |
| :--- | :--- | :--- |
| **[`builtin:assign`](../../reference/builtins/assign.md)** | 将变量、结构体字段路径（如 `request.items`）或常量字面量绑定写入顶层变量 `value` | 无分支单出边，输出目标变量 `value` |
| **[`builtin:array_shift`](../../reference/builtins/array-shift.md)** | 从列表头部弹出首元素 `item` 与剩余切片 `rest` | • **`.some`**：队列非空，`item` 与 `rest` 获得定值<br>• **`.empty`**：队列已空，不写入 `item` 与 `rest` |
| **[`builtin:array_append`](../../reference/builtins/array-append.md)** | 将单步产出的元素 `item` 追加至列表 `source` 末尾 | 无分支单出边，输出更新后的列表 `result` |
| **[`builtin:map_set`](../../reference/builtins/map-set.md)** | 将键值对 `key -> value` 插入或更新至有序映射 `source` | 无分支单出边，输出更新后的有序映射 `result` |
| **[`builtin:map_get`](../../reference/builtins/map-get.md)** | 按动态键 `key` 从有序映射 `source` 中查找对应值（可选配 `default`） | • **省略 `default`**：暴露 **`.some`**（定值 `value`）与 **`.none`**<br>• **传入 `default`**：无分支单出边，输出 `value` |
| **[`builtin:map_delete`](../../reference/builtins/map-delete.md)** | 按动态键 `key` 从有序映射 `source` 中移除条目并返回新副本 | 无分支单出边，输出更新后的有序映射 `result` |
| **[`builtin:object_merge`](../../reference/builtins/object-merge.md)** | 构造新的 `TypedDict` 或基于已有结构体 `base` 不可变覆盖更新指定字段 | 无分支单出边，输出结构体变量 `result` |

### 1. 队列初始化、弹出与列表追加片段（`assign` + `array_shift` + `array_append`）

当待消费的列表位于输入结构体内部（如 `batch_request.urls`）时，先通过 `builtin:assign` 将其提取至顶层变量 `pending_urls`，再将 `builtin:array_shift` 的 `with.output.rest` 与 `with.input.source` 绑定为同一个顶层变量 `pending_urls`，即可在每轮回边中原地推进剩余队列切片；结合 `builtin:array_append` 可在每轮失败时将错误日志追加至历史列表：

```yaml
graph:
  variables:
    # 初始化用于累积失败记录的空列表（需显式声明 TypeSpec 与 default: []）
    error_logs:
      type: array
      items: { type: string }
      default: []

  nodes:
    # 0. 入环前将结构体字段 batch_request.urls 赋值给顶层队列变量 pending_urls
    init_url_queue:
      use: builtin:assign
      with:
        input:
          source: batch_request.urls
        output:
          value: pending_urls

    # 1. 从队列头部弹出首元素 current_url，并将剩余切片写回 pending_urls
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
    - pop_url.empty -> end.exhausted     # 队列已空：跳出循环
```

### 2. 有序映射读写与结构体不可变更新片段（`map_get` / `map_set` + `object_merge`）

当需要在循环中按动态键查询或更新有序映射（`type: object` + `additionalProperties`），或不可变地更新结构体（`TypedDict`）的某个字段时，组合使用 `builtin:map_get`、`builtin:map_set` 与 `builtin:object_merge`：

```yaml
graph:
  variables:
    # 初始化空有序映射
    probe_results:
      type: object
      additionalProperties: { type: string }
      default: {}

  nodes:
    # 1. 按动态键名 current_host 查找缓存字典，未命中时回退为 "unknown"
    read_previous_status:
      use: builtin:map_get
      with:
        input:
          source: probe_results
          key: current_host
          default: { value: "unknown" }
        output:
          value: prev_status

    # 2. 将最新探测结果写入有序映射 probe_results
    record_probe_status:
      use: builtin:map_set
      with:
        input:
          source: probe_results
          key: current_host       # 动态键名（type: string）
          value: current_status   # 对应键值
        output:
          result: probe_results   # 原地写回更新后的有序映射副本

    # 3. 基于已有结构体 host_summary 不可变更新 last_status 字段并写回 host_summary
    update_host_summary:
      use: builtin:object_merge
      with:
        input:
          base: host_summary
          last_status: current_status
        output:
          result: host_summary
```

---

## 三、 完整示例：生成-校验-修正循环

以下完整工作流通过 `graph.variables` 初始化跨轮状态，并在校验未通过时经由成环回边跳回生成节点，直至校验通过后退出：

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

  # 3. 初始化跨轮次状态变量：首轮进入 generate_candidate 时读取此初值
  variables:
    attempts: 0
    last_error: ""

  nodes:
    # 循环体步骤 1：读取需求、上轮错误信息与当前计数，产出候选结果并递增 attempts
    generate_candidate:
      input: [task_spec, last_error, attempts]
      output:
        candidate: { type: string }
        attempts: { type: integer }
      run: "根据需求 variables.task_spec 与上次报错 variables.last_error 生成代码写入 output.candidate，并将 variables.attempts + 1 写入 output.attempts。"

    # 循环体步骤 2：校验候选代码
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
    - start -> generate_candidate
    - generate_candidate -> verify_candidate
    # 成环回边：将更新后的 last_error 与 attempts 带回 generate_candidate 进入下一轮
    - verify_candidate.retry -> generate_candidate
    # 退环出口：candidate 与 attempts 均已定值，正常结束工作流
    - verify_candidate.pass -> end
```

> 包含最大重试次数早退分支及队列回退链的生产级模板，可参阅 Cookbook：
> * [生成-校验-修正闭环](../../cookbook/01-loops-and-queues/01-generate-verify-refine-loop.md)
> * [候选节点短路早退回退链](../../cookbook/01-loops-and-queues/02-short-circuit-fallback-chain.md)

---

下一节：[01. 同构结构化并发：集合遍历（`for_each`）](../03-concurrency/01-for-each.md)。
