# RFC-0005: 确定性比较谓词、`for_each` 早退控制（`break`）与异常诊断上下文

* **状态**：Rejected（不采纳）
* **所属分类**：对标主流编程语言的原语与内建算子扩展（Language Expressiveness & Builtin Extensions）
* **核心解决的问题**：增强 `builtin:switch`（支持 `integer` 枚举与 `default` 分支）并新增 `builtin:compare`；新增串行 `for_each` 的 `break_on` 早退机制；支持 `catch` 绑定结构化错误诊断上下文。
* **关联规范模块**：
  * [`02-type-system-and-symbols/05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md)
  * [`04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md`](../manual/spec/04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md)
  * [`04-runtime-execution-model/04-exception-handling-and-unwinding.md`](../manual/spec/04-runtime-execution-model/04-exception-handling-and-unwinding.md)

---

## 一、 问题背景与能力缺口

### 痛点 5.1：缺少确定性比较谓词，`builtin:switch` 仅支持 `boolean` 与穷尽 `string enum`
1. **基础比较运算缺失**：
   在主流语言中，`if (score >= 0.8)`、`if (retry_count < max_retries)`、`if (author_id == current_user_id)` 是最基础的确定性控制流。而在当前 Axcutor 规范中，由于没有任何比较算子（`builtin:compare`），哪怕只是判断两个整数或字符串是否相等、或者判断数值是否达到阈值，都必须编写一个外部 `run` 节点。
2. **`builtin:switch` 不支持 `default` 分支与普通标量匹配**：
   当前 `builtin:switch` 要求输入必须是 `boolean` 或带 `enum` 的 `string`，且必须为每个枚举值连边。若输入是没有 `enum` 约束的普通 `string` 或 `integer`（如 HTTP 状态码、业务类型码），或者 `enum` 有 10 个枚举值但只需特判其中 2 个、其余 8 个走默认分支，当前 `builtin:switch` 无法表达。

---

### 痛点 5.2：串行 `for_each`（`concurrency: 1`）缺少早退机制（`break`）
* 在 [`02-fork-join-and-for-each-scheduler.md`](../manual/spec/04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md) 中，`for_each` 只有两种终止方式：
  1. 跑满全部 $n$ 个元素后正常提交 `filter`、`collect`、`state`；
  2. 某次迭代抛出未捕获异常，触发 Fail-Fast 取消并**彻底丢弃已累积的全部 `filter`、`collect`、`state` 缓冲槽位**，向外层抛出异常。
* 在实际编程中，极其常见的模式是**带早退的串行搜索或预算受限折叠**（例如：按优先级遍历候选列表，一旦找到第一个验证通过的结果或累积 token 达到上限，立即 `break` 停止后续元素的处理，并正常保留目前已收集的 `collect` / `state` 结果）。
* 目前若要实现 `break`，用户必须放弃 `for_each`，改用 `builtin:array_shift` + 成环回边手写 `while` 循环，代码复杂度大幅上升。

---

### 痛点 5.3：`catch` 异常恢复分支与 `-> fail` 丢失全部错误诊断上下文
* 在 [`04-exception-handling-and-unwinding.md`](../manual/spec/04-runtime-execution-model/04-exception-handling-and-unwinding.md) 中：
  * 当节点通过 `catch` 捕获可恢复异常（如 `fallback: "*"` 或 `failed: [execution, timeout]`）时，`MaterializeCatch` 状态强制规定 `output: []`（不向栈帧写入任何变量）；
  * 当子图通过 `src -> fail` 主动回滚时，仅向父节点抛出一个无载荷的 `execution` 符号。
* **导致的工程问题**：
  * 在生产级工作流（如 [`02-batch-isolation-and-dead-letter-queue.md`](../manual/docs/cookbook/03-transactions-and-resilience/02-batch-isolation-and-dead-letter-queue.md) 死信队列或事务补偿通知）中，进入 `catch` 分支后的补偿节点完全拿不到**失败原因（`message`）**、**实际触发的异常类别（`category`：究竟是 `timeout`、`contract` 还是 `execution`）** 以及 **发生故障的内部节点名（`node_id`）**，无法生成有意义的告警、审计日志或根据错误详情做精细补偿。

---

## 二、 解决方案设计

### 方案 5.1：新增 `builtin:compare` 确定性比较算子并增强 `builtin:switch`

#### 1. 新增 `builtin:compare`
提供零开销的二元比较谓词算子，暴露 `.true` 与 `.false` 两个静态分支（`output_subset = \emptyset`）：

```yaml
check_threshold:
  use: builtin:compare
  with:
    input:
      left: current_score            # 变量、FieldPathExpr 或常量
      op: { value: "gte" }           # 枚举: "eq" | "ne" | "lt" | "lte" | "gt" | "gte"
      right: 0.85                    # 变量、FieldPathExpr 或常量
```

* **编译期类型约束**：
  * `op`（必填）：必须为 `{ value: "<op>" }` 字符串常量，取值属于 `{"eq", "ne", "lt", "lte", "gt", "gte"}`；
  * 当 `op` 为 `"eq"` 或 `"ne"` 时：`left` 与 `right` 的基础类型必须兼容（允许同类型标量、数组或对象深度相等比较，或允许可空类型与 `null` 比较）；
  * 当 `op` 为 `"lt" | "lte" | "gt" | "gte"` 时：`left` 与 `right` 必须均为非空数值类型（`integer` 或 `number`，允许 `integer` 与 `number` 之间直接比较大小）或均为非空 `string`（按字典序比较）。
* **静态控制分支推导**：暴露 `.true` 与 `.false`（`output_subset = \emptyset`）。

#### 2. 增强 `builtin:switch`（可选 `cases` 与 `default` 分支）
在保留现有 `boolean` 与穷尽 `string enum` 零配置推导的基础上，允许在 `with.input` 中可选声明 `cases: { value: { <branch_id>: <scalar_literal>, ... } }`：
* 当声明了 `cases` 时，`source` 允许为任意非空标量（`string`、`integer`、`boolean`）；
* 自动暴露 `cases` 中定义的全部 `<branch_id>` 分支，并额外暴露 `.default` 兜底分支（当 `source` 为 `enum` 且 `cases` 已穷尽全部枚举值时，无需暴露 `.default`）。

---

### 方案 5.2：在串行 `for_each` 中支持 `break_on` 正常早退修饰符

在 [`ForEachSpec`](../manual/docs/reference/01-node-and-graph-fields.md) 中新增可选字段 `break_on: List<BranchOrCatchId>`：

```yaml
find_first_valid_provider:
  for_each:
    source: candidate_providers
    item: provider
    concurrency: 1                   # 使用 break_on 时强制要求 concurrency: 1
    state: [selected_response]
    break_on: [matched]              # 一旦某次迭代返回 .matched 分支，完成当次收敛后立即停止后续迭代
  input: [provider, selected_response]
  output:
    selected_response:
      type: [string, "null"]
  branches:
    matched:
      description: "成功获取有效响应，触发早退"
      output: [selected_response]
    try_next:
      description: "当前候选不可用，继续尝试下一个"
      output: [selected_response]
  run: "..."
```

* **编译期静态约束**：
  1. **串行排他约束**：若声明了非空 `break_on`，必须满足 `for_each.concurrency == 1`（若显式声明 `concurrency > 1` 则编译期报错，确保早退边界与确定性保序语义完全自洽）；
  2. **分支存在性约束**：$\text{set}(\text{break\_on}) \subseteq \text{dom}(N.\text{branches}) \cup \text{dom}(N.\text{catch})$。
* **运行期状态机语义**：
  * 在按序执行 $i = 0, 1, \dots, n-1$ 的过程中，当第 $k$ 次迭代完成并选定分支 $\text{choice}_k$ 后：
    1. 正常将第 $k$ 次迭代的结果写入 $\text{Slots}[k]$ 并更新 `state`；
    2. 若 $\text{choice}_k \in \text{set}(\text{break\_on})$，立即终止后续 $i = k+1 \dots n-1$ 的迭代（后续未启动的元素不再计入 `steps(F_G)`）；
    3. 仅对已执行的有效槽位 $\text{Slots}[0 \dots k]$ 执行 `filter`、`collect` 与 `state` 汇聚，并原子提交至外层图栈帧，沿默认出边 `N -> target` 正常前进。

---

### 方案 5.3：结构化异常诊断上下文绑定（`catch.error_into`）

为了在不破坏现有 `catch: { <branch>: <selectors> }` 简洁性的前提下让恢复路径获得错误详情，支持在 `catch` 分支定义中使用扩展对象形态绑定标准错误结构体 `ErrorInfo`：

```yaml
nodes:
  query_upstream:
    timeout_ms: 3000
    retry:
      max_attempts: 2
      on: [execution, timeout]
    catch:
      # 简写形态（保持 100% 向后兼容，不绑定错误变量）：
      # fallback: "*"
      # 扩展形态：将捕获到的异常诊断信息写入顶层变量 upstream_err
      fallback:
        on: "*"
        error_into: upstream_err
    input: [request_payload]
    output:
      response_data: { type: string }
    run: "..."

  log_and_compensate:
    # 下游恢复节点可直接读取强类型的 upstream_err.category 与 upstream_err.message
    input: [request_payload, upstream_err]
    output:
      response_data: { type: string }
    run: "主查询因 variables.upstream_err.category (variables.upstream_err.message) 失败，生成兜底数据写入 output.response_data。"
```

* **标准 `ErrorInfo` 静态类型（编译器内置 `TypeSpec`）**：
  当 `catch.<branch>` 声明了 `error_into: <err_var>` 时，编译器自动向当前图符号表 $\Sigma_G$ 注册 `<err_var>`，其 `TypeSpec` 固定为：
  ```yaml
  type: object
  properties:
    category:
      type: string
      enum: [execution, timeout, contract, max_steps]
    message:
      type: string
    node_id:
      type: string
  required: [category, message, node_id]
  ```
* **必达定值分析传递方程更新**：
  * 对于声明了 `error_into: <err_var>` 的异常分支 $c$，其边级传递方程为：
    $$Def_{\text{out}}(N.c) = Def_{\text{in}}(N) \cup \{\text{err\_var}\}$$
  * 当 `catch.<branch>` 被单次 `for_each` 迭代内部消费时，若声明了 `error_into: <err_var>`，`<err_var>` 视为该次迭代在异常分支 $c$ 上产出的单次局部字段（允许外层 `for_each.collect` 将所有失败元素的 `<err_var>` 收集为 `Array<ErrorInfo>` 死信错误日志列表！）。
