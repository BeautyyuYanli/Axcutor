# 01. Branches：排他条件分支与按需产出

在业务流程中，节点通常需要根据执行结果走向不同控制分支（如校验通过或驳回、命中缓存或回源查询），且不同分支往往产出不同的字段子集。

Axcutor 通过 **`branches`（排他控制分支）** 与 **分支关联输出契约（Branch-Scoped Output / Tagged Union）**，支持每个分支仅声明并产出该分支所需的非空字段，无需将互斥分支的字段声明为 `[T, "null"]` 或填充 `null` 占位值。

---

## 一、 `branches` 的两种声明形式

在根节点、图内 `run` 节点或内联 `graph` 节点上，均可声明 `branches`。

### 1. 全量产出简写（`<choice>: "<description>"`）

当各分支均产出节点 `output` 中的全部字段，或节点仅执行条件路由而不产出 `output` 字段时，直接将分支值写为描述字符串：

```yaml
verify_signature:
  input: [payload, signature]
  # 节点无 output，两个分支仅用于控制流路由
  branches:
    valid: "签名校验通过"
    invalid: "签名不匹配或已过期"
  run: "校验 variables.payload 的签名 variables.signature，有效则选择 valid，否则选择 invalid。"
```

### 2. 分支按需产出子集（`<choice>: { description: "...", output: [<field>, ...] }`）

当不同分支产出不同字段子集时，通过 `output: [<field>, ...]` 显式指定该分支承诺产出的字段列表（若某分支不产出任何字段，可写 `output: []`）：

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
  run: "审核 variables.order_request；若合规写入 output.approved_invoice 并选择 approved；否则写入 output.reject_reason 并选择 rejected。均写入时间戳 output.audit_timestamp。"
```

---

## 二、 分支连边与分支终点（`end.<branch>`）

### 1. 穷尽分支出边（`node.<choice> -> target`）

未挂载 `for_each` 的有分支节点，必须在 `graph.edges` 中为每一个声明的分支编写唯一一条出边。各分支下游路径仅获得该分支 `output` 子集的定值：

```yaml
edges:
  - start -> inspect_order
  # approved 分支下游可安全读取 approved_invoice 与 audit_timestamp
  - inspect_order.approved -> issue_shipment
  # rejected 分支下游可安全读取 reject_reason 与 audit_timestamp
  - inspect_order.rejected -> send_rejection_notice
```

### 2. 分支出口终点（`-> end.<branch>`）

当根工作流或内联 `graph` 自身声明了 `branches` 时，图内控制流通过 `-> end.<branch>` 退出。通往 `end.<branch>` 的路径只需确保该分支关联的 `output` 子集已定值，其他分支专属的输出变量无需赋值：

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

## 三、 确定性路由、拆包、查找与定值内建算子

当图中变量已经是明确的 `boolean`、带 `enum` 约束的 `string`、显式可空类型 `Optional<T>`（`type: [<BaseType>, "null"]`，含 `TypedDict` 中未列入 `required` 的可选叶子字段）或有序映射 `OrderedMap<string, V>` 时，可直接调用运行时内建算子执行零开销路由、拆包、按键查找或分支常量定值（完整参数规范见 [内建节点字典](../../reference/builtins/index.md)）。

### 1. [`builtin:switch`](../../reference/builtins/switch.md)：布尔与枚举确定性路由

* 绑定 `type: boolean` 变量或结构体字段路径时，自动暴露 **`.true`** 与 **`.false`** 分支。
* 绑定 `type: string, enum: [<v1>, <v2>, ...]` 变量或结构体字段路径时，要求每个枚举值 `<vi>` 均为合法分支标识符（匹配 `^[a-zA-Z_][a-zA-Z0-9_]*$` 且不属于 `start`/`end`/`fail`），自动暴露与各枚举值同名的 **`.<v1>`**、**`.<v2>`** 分支。

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

### 2. [`builtin:unwrap`](../../reference/builtins/unwrap.md)：可空类型拆包与默认值合并

`builtin:unwrap` 将可空类型 `[T, "null"]`（包括显式声明为 `[T, "null"]` 的变量/字段，以及在 `TypedDict.properties` 中声明但未列入 `required`、投影时自动提升为 `[T, "null"]` 的可选叶子字段）收窄为非空类型 `T`，支持两种模式：

* **模式 A（省略 `default`）**：暴露 **`.some`** 与 **`.none`** 两个分支；在 `.some` 分支下产出非空类型 `T` 的目标变量 `value`，在 `.none` 分支下不产出 `value`（`output: []`，且从定值集反定值移除 `value`）。
* **模式 B（传入 `default`）**：无分支单出边（`node -> target`）；当 `source` 为 `null`（或可选字段缺失）时自动回退为非空 `default` 值，直接产出非空类型 `T` 的 `value`。

```yaml
nodes:
  # 模式 A（省略 default）：条件拆包，暴露 .some（产出 reviewer_id）与 .none（不产出）
  check_assigned_reviewer:
    use: builtin:unwrap
    with:
      input:
        source: ticket.optional_reviewer
      output:
        value: reviewer_id

  # 模式 B（传入 default）：默认值合并，无分支单出边，直接产出非空 string 变量 operator_name
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

### 3. [`builtin:map_get`](../../reference/builtins/map-get.md) 与 [`builtin:assign`](../../reference/builtins/assign.md)：字典条件查找与分支常量定值

* **[`builtin:map_get`](../../reference/builtins/map-get.md)**：按动态键 `key` 查询有序映射 `OrderedMap<string, V>`。省略 `default` 时暴露 **`.some`**（键存在，定值 `value`）与 **`.none`**（键不存在，反定值移除 `value`）两个分支；传入 `default` 时收敛为无分支单出边。
* **[`builtin:assign`](../../reference/builtins/assign.md)**：将变量、结构体字段路径或常量字面量直接绑定写入目标顶层变量 `value`，常用于在分支合流前无需调用 `run` 即可完成变量的默认定值或字段提取。

```yaml
nodes:
  # 按动态键 query_id 查找缓存字典 answer_cache：存在走 .some，不存在走 .none
  lookup_cached_answer:
    use: builtin:map_get
    with:
      input:
        source: answer_cache
        key: query_id
      output:
        value: final_answer

  # 在无需计算的分支上，使用 builtin:assign 直接将常量赋给汇合变量 audit_tag
  set_cache_hit_tag:
    use: builtin:assign
    with:
      input:
        source: { value: "cache_hit" }
      output:
        value: audit_tag
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

# 2. 根节点输出全集：各字段均为非空 string
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
      run: "由 variables.assigned_operator 处理常规工单 variables.ticket.id，结果写入 output.resolution_report。"

    # 步骤 3b：紧急分支处理节点，仅产出 escalated 出口所需的 escalation_id
    handle_urgent:
      input: [ticket, assigned_operator]
      output:
        escalation_id: { type: string }
      run: "由 variables.assigned_operator 升级紧急工单 variables.ticket.id，升级单号写入 output.escalation_id。"

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
