# 01. Branches：排他条件分支与按需产出

在业务流程中，节点常需根据执行结果走向不同的控制分支（如校验通过或驳回、命中缓存或回源查询），且不同分支往往只产出各自所需的字段子集。

Axcutor 通过 **`branches`（排他控制分支）** 与 **分支按需产出契约（Branch-Scoped Output）**，允许每个分支仅声明并产出当前路径实际生成的非空字段，无需将互斥分支的专属字段妥协声明为可空类型 `[T, "null"]` 或填充 `null` 占位值。

---

## 一、 `branches` 的两种声明形式

在根节点、图内 `run` 节点或内联 `graph` 节点上，均可声明 `branches`。

### 1. 全量产出简写（`<choice>: "<description>"`）

当各分支均产出节点 `output` 中的全部字段，或节点仅执行条件路由而不产出任何 `output` 字段时，直接将分支映射为描述字符串：

```yaml
verify_signature:
  input: [payload, signature]
  # 节点无 output，两个分支仅用于控制流路由
  branches:
    valid: "签名校验通过"
    invalid: "签名不匹配或已过期"
  run:
    executor: builtin
    content: |
      if input.signature.startswith("sig_") and len(input.payload) > 0:
          return Branch.valid()
      return Branch.invalid()
```

### 2. 分支按需产出子集（`<choice>: { description: "...", output: [<field>, ...] }`）

当不同分支产出不同的字段子集时，通过 `output: [<field>, ...]` 显式声明该分支承诺产出的字段列表（若某分支不产出任何字段，可声明 `output: []`）：

```yaml
inspect_order:
  input: [order_request]
  output:
    approved_invoice: { type: string }  # 非空 string，仅在 approved 分支产出
    reject_reason: { type: string }     # 非空 string，仅在 rejected 分支产出
    audit_timestamp: { type: integer }  # 两个分支均产出的公共字段
  branches:
    approved:
      description: "订单合规，签发核准发票号"
      output: [approved_invoice, audit_timestamp]
    rejected:
      description: "订单不合规，记录驳回原因"
      output: [reject_reason, audit_timestamp]
  run:
    executor: builtin
    content: |
      if input.order_request.amount > 0:
          return Branch.approved(
              approved_invoice=f"INV-{input.order_request.id}",
              audit_timestamp=1700000000,
          )
      return Branch.rejected(
          reject_reason="Invalid order amount",
          audit_timestamp=1700000000,
      )
```

---

## 二、 分支连边与分支终点（`end.<branch>`）

### 1. 穷尽分支出边与定值隔离（`node.<choice> -> target`）

未挂载 `for_each` 的有分支节点，必须在 `graph.edges` 中为每一个声明的分支编写唯一一条出边。

编译器按分支精确追踪变量的定值状态：沿 `node.<choice>` 出边进入下游时，仅将该分支 `output` 列表中的字段标记为**已定值（Gen）**，同时将节点 `output` 中未列入该分支的其他字段执行**反定值（Kill，即从当前路径的已定值集合中移除）**。这一机制从静态编译期杜绝了下游节点误读未命中分支专属字段或残留旧值的可能：

```yaml
edges:
  - start -> inspect_order
  # approved 分支下游仅定值 approved_invoice 与 audit_timestamp（reject_reason 未定值，不可读取）
  - inspect_order.approved -> issue_shipment
  # rejected 分支下游仅定值 reject_reason 与 audit_timestamp（approved_invoice 未定值，不可读取）
  - inspect_order.rejected -> send_rejection_notice
```

### 2. 分支出口终点（`-> end.<branch>`）

当根工作流或内联 `graph` 自身声明了 `branches` 时，图内控制流通过 `-> end.<branch>` 退出。通往 `end.<branch>` 的路径只需确保该分支声明的 `output` 子集均已定值，其他分支的专属输出字段无需赋值：

```yaml
output:
  shipment_id: { type: string }
  reject_reason: { type: string }
branches:
  fulfilled:
    description: "订单已发货"
    output: [shipment_id]
  rejected:
    description: "订单被驳回"
    output: [reject_reason]

graph:
  nodes: ...
  edges:
    - start -> inspect_order
    - inspect_order.approved -> issue_shipment
    # 通往 end.fulfilled 的路径仅需为 shipment_id 赋值，无需为 reject_reason 赋值
    - issue_shipment -> end.fulfilled
    # 通往 end.rejected 的路径仅需为 reject_reason 赋值，无需为 shipment_id 赋值
    - inspect_order.rejected -> end.rejected
```

---

## 三、 确定性条件路由与可空拆包算子

当图中的条件分流与可空值判空不涉及外部 I/O 或复杂推理时，可直接调用内建算子执行**零外部执行器开销**的确定性路由与拆包（所有内建算子的输出目标变量均须在当前图符号表中显式声明类型，完整参数规范见 [内建节点字典](../../reference/builtins/index.md)）。

> 若需在多条分支汇合前为公共变量赋予常量初值或拷贝字段，可使用 [`builtin:assign`](../../reference/builtins/assign.md) 直接完成定值而无需调用 `run` 节点；关于 `builtin:assign` 以及 `builtin:map_get`、`builtin:array_get` 等容器查找与数据操作算子的详细用法，请参阅 [03. Builtins：确定性内建算子与数据操作](./03-builtin-operators.md)。

### 1. [`builtin:switch`](../../reference/builtins/switch.md)：布尔与枚举多路路由

* **布尔路由**：绑定 `type: boolean` 变量或结构体字段路径时，自动暴露 **`.true`** 与 **`.false`** 两个分支。
* **枚举多路路由**：绑定 `type: string, enum: [<v1>, <v2>, ...]` 变量或结构体字段路径时，要求每个枚举值 `<vi>` 均为合法受限标识符（匹配 `^[a-zA-Z_][a-zA-Z0-9_]*$` 且不为控制流保留字或 Python 硬关键字），自动暴露与各枚举值同名的 **`.<v1>`**、**`.<v2>`** 分支。

```yaml
nodes:
  # 布尔二路路由：自动暴露 check_vip.true 与 check_vip.false
  check_vip:
    use: builtin:switch
    with:
      input:
        source: customer.is_vip

  # 枚举多路路由：若 priority 的 enum 为 [low, medium, high]，自动暴露对应三个分支
  route_priority:
    use: builtin:switch
    with:
      input:
        source: ticket.priority

edges:
  - start -> check_vip
  - check_vip.true -> route_priority
  - check_vip.false -> standard_queue
  - route_priority.high -> urgent_handler
  - route_priority.medium -> normal_handler
  - route_priority.low -> batch_handler
```

### 2. [`builtin:compare`](../../reference/builtins/compare.md) 与 [`builtin:logic`](../../reference/builtins/logic.md)：比较与逻辑分支路由

* **[`builtin:compare`](../../reference/builtins/compare.md)**：支持相等性比较（`eq`、`ne`，按 JSON 深度值判等，且允许可空变量与 `null` 比较）与非空标量序比较（`lt`、`lte`、`gt`、`gte`，支持 `integer`/`number` 数值比较与 `string` 字典序比较）。省略 `with.output` 时直接作为路由节点暴露 **`.true`** / **`.false`** 分支；声明 `with.output.result` 时将布尔结果写入目标变量（收敛为无分支单出边）。
* **[`builtin:logic`](../../reference/builtins/logic.md)**：对非空 `boolean` 执行逻辑与（`and`）、逻辑或（`or`）或逻辑非（`not`）。同样支持省略 `with.output` 直接暴露 **`.true`** / **`.false`** 分支，或声明 `with.output.result` 将布尔结果写入变量。

```yaml
graph:
  variables:
    is_high_confidence:
      type: boolean

  nodes:
    # 1. 将数值阈值判断 (review_score >= 0.85) 的布尔结果存入变量 is_high_confidence
    eval_confidence:
      use: builtin:compare
      with:
        input:
          op: { value: "gte" }
          left: review_score
          right: 0.85
        output:
          result: is_high_confidence

    # 2. 组合两个布尔条件 (is_high_confidence and is_verified)，省略 with.output 直接作为分支路由
    check_auto_approve:
      use: builtin:logic
      with:
        input:
          op: { value: "and" }
          left: is_high_confidence
          right: is_verified

  edges:
    - start -> eval_confidence
    - eval_confidence -> check_auto_approve
    - check_auto_approve.true -> auto_publish
    - check_auto_approve.false -> manual_review
```

### 3. [`builtin:unwrap`](../../reference/builtins/unwrap.md)：可空类型拆包与默认值合并

`builtin:unwrap` 将可空类型 `[T, "null"]`（包括显式声明为 `[T, "null"]` 的变量/字段，以及 `TypedDict.properties` 中未列入 `required`、在路径投影时自动提升为 `[T, "null"]` 的可选字段）收窄为非空类型 `T`，支持两种工作模式：

* **条件拆包模式（省略 `default`）**：暴露 **`.some`** 与 **`.none`** 两个分支。当源值非空时走 `.some` 分支，将拆包后的非空值写入目标变量 `value`；当源值为 `null`（或可选字段缺失）时走 `.none` 分支，不产出 `value`（`output: []`，并在该分支路径上对 `value` 执行反定值，防止下游误读旧值）。
* **默认值合并模式（传入 `default`）**：收敛为无分支单出边（`node -> target`）。当 `source` 为 `null`（或可选字段缺失）时自动回退为非空 `default` 值，始终产出非空类型 `T` 的目标变量 `value`。

```yaml
graph:
  # 显式声明由 builtin:unwrap 写入的目标变量类型
  variables:
    reviewer_id: { type: string }
    operator_name: { type: string }

  nodes:
    # 条件拆包（省略 default）：暴露 .some（定值 reviewer_id）与 .none（不产出并反定值 reviewer_id）
    check_assigned_reviewer:
      use: builtin:unwrap
      with:
        input:
          source: ticket.optional_reviewer
        output:
          value: reviewer_id

    # 默认值合并（传入 default）：无分支单出边，始终产出非空 string 变量 operator_name
    resolve_operator_with_default:
      use: builtin:unwrap
      with:
        input:
          source: ticket.operator_override
          default: { value: "auto_bot" }
        output:
          value: operator_name

  edges:
    - start -> resolve_operator_with_default
    - resolve_operator_with_default -> check_assigned_reviewer
    - check_assigned_reviewer.some -> notify_specific_reviewer
    - check_assigned_reviewer.none -> assign_from_pool
```

---

## 四、 完整示例：组合 `builtin:unwrap`、`builtin:switch` 与按需分支出口

以下完整工作流对输入的工单结构体先执行可空字段默认值合并，再按优先级枚举值路由至不同处理分支，并从对应的 `end.<branch>` 退出：

```yaml
# 1. 根节点输入契约：包含 enum 枚举字段与可空 [string, "null"] 字段
input:
  ticket:
    type: object
    properties:
      id: { type: string }
      priority:
        type: string
        enum: [normal, urgent]
      operator_override:
        type: [string, "null"]
    required: [id, priority, operator_override]

# 2. 根节点输出全集：各字段均为非空 string（已并入图级符号表）
output:
  resolution_report: { type: string }
  escalation_id: { type: string }
  assigned_operator: { type: string }

# 3. 根节点排他分支契约：resolved 与 escalated 分别关联不同的输出字段子集
branches:
  resolved:
    description: "常规工单直接处理完毕"
    output: [resolution_report, assigned_operator]
  escalated:
    description: "紧急工单已转交专家队列"
    output: [escalation_id, assigned_operator]

graph:
  nodes:
    # 步骤 1：使用带 default 的 builtin:unwrap 将可空字段 ticket.operator_override 合并为非空 assigned_operator
    resolve_operator:
      use: builtin:unwrap
      with:
        input:
          source: ticket.operator_override
          default: { value: "auto_bot" }
        output:
          value: assigned_operator

    # 步骤 2：使用 builtin:switch 对枚举字段 ticket.priority (normal | urgent) 执行二路分流
    route_priority:
      use: builtin:switch
      with:
        input:
          source: ticket.priority

    # 步骤 3a：常规分支处理节点，仅产出 resolved 出口所需的 resolution_report
    handle_normal:
      input: [ticket, assigned_operator]
      output:
        resolution_report: { type: string }
      run:
        executor: builtin
        content: |
          return Output(
              resolution_report=f"Ticket {input.ticket.id} resolved by {input.assigned_operator}"
          )

    # 步骤 3b：紧急分支处理节点，仅产出 escalated 出口所需的 escalation_id
    handle_urgent:
      input: [ticket, assigned_operator]
      output:
        escalation_id: { type: string }
      run:
        executor: builtin
        content: |
          return Output(
              escalation_id=f"ESC-{input.ticket.id}-{input.assigned_operator}"
          )

  # 4. 控制流连边：两条分支路径分别汇聚至 end.resolved 与 end.escalated
  edges:
    - start -> resolve_operator
    - resolve_operator -> route_priority
    - route_priority.normal -> handle_normal
    - handle_normal -> end.resolved
    - route_priority.urgent -> handle_urgent
    - handle_urgent -> end.escalated
```

---

下一节：[02. 成环回边：动态条件循环（While Loop）](./02-cycles-and-back-edges.md)。
