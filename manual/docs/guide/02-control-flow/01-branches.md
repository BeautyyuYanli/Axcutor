# 01. Branches：排他条件分支与按需产出

在业务编排中，节点常常需要根据执行结果走向不同的分支（如校验通过 vs. 驳回、存在可选值 vs. 为 `null`）。更重要的是，**不同分支往往产出不同的字段子集**。

Axcutor 通过 **`branches`（排他控制分支）** 结合 **分支关联输出契约（Branch-Scoped Output / Tagged Union）**，让你在每个分支只产出该分支真正需要的非空字段，彻底告别用 `[T, "null"]` 和 `null` 假初值污染正常业务流。

---

## 一、 声明 `branches` 的两种形式

在根节点、图内 `run` 节点或内联 `graph` 节点上，均可声明 `branches`：

### 1. 全量产出简写（`<choice>: "<description>"`）
当各分支均产出节点 `output` 中的全部字段（或节点无 `output`，仅做条件判断）时，直接写描述字符串：

```yaml
verify_signature:
  input: [payload, signature]
  branches:
    valid: "签名有效"
    invalid: "签名不匹配或已过期"
  run: "校验 variables.payload 的签名 variables.signature，有效则选 valid，否则选 invalid。"
```

### 2. 分支按需产出子集（`<choice>: { description: "...", output: [<field>, ...] }`）
当不同分支产出不同的字段时，用 `output: [...]` 列出该分支承诺产出的字段名（若某分支不产出任何字段，可写 `output: []`）：

```yaml
inspect_order:
  input: [order_request]
  output:
    approved_invoice: { type: string }  # 纯正非空 string，仅在 approved 分支产出
    reject_reason: { type: string }     # 纯正非空 string，仅在 rejected 分支产出
    audit_timestamp: { type: integer }  # 两个分支均产出的公共字段
  branches:
    approved:
      description: "订单合规，签发核准发票号"
      output: [approved_invoice, audit_timestamp]
    rejected:
      description: "订单不合规，记录驳回原因"
      output: [reject_reason, audit_timestamp]
  run: "审核 variables.order_request；若合规写入 output.approved_invoice 并选 approved；否则写入 output.reject_reason 并选 rejected。均记录时间戳 output.audit_timestamp。"
```

---

## 二、 分支连边与分支终点（`end.<branch>`）

1. **穷尽分支出边**：声明了 `branches` 的节点（未挂载 `for_each` 时），在 `graph.edges` 中必须为**每一个分支**编写一条出边 `node.<choice> -> target`。
2. **分支终点 `end.<branch>`**：当根工作流或内联 `graph` 自身声明了 `branches` 时，图内控制流通过 `-> end.<branch>` 退出。到达 `end.<branch>` 的路径**只需确保该分支 `output` 列表中的变量已赋值**，其他分支专属的输出变量无需赋值！

---

## 三、 零开销确定性路由与拆包算子（`builtin:switch` & `builtin:unwrap`）

当图中的某个变量已经是明确的 `boolean`、带 `enum` 约束的 `string`，或者是显式可空类型 `Optional<T>`（`type: [<BaseType>, "null"]`）时，无需编写 `run` 节点，直接使用运行时内建算子即可完成零开销路由与非空拆包（完整参数字典见 [内建节点字典](../../reference/builtins/README.md)）：

* **[`builtin:switch`](../../reference/builtins/switch.md)**：
  * 绑定 `boolean` 变量/字段路径时，自动暴露 **`.true`** 与 **`.false`** 分支；
  * 绑定 `{ type: string, enum: [low, medium, high] }` 变量/字段路径时，自动暴露 **`.low`**、**`.medium`**、**`.high`** 分支。
* **[`builtin:unwrap`](../../reference/builtins/unwrap.md)**：
  * **模式 A（省略 `default`）**：暴露 **`.some`** 与 **`.none`** 分支；在 `.some` 分支下将可空类型 `[T, "null"]` 拆包收窄为非空类型 `T` 的新变量。
  * **模式 B（传入 `default`）**：无分支单出边；当源变量为 `null` 时自动取 `default` 值，直接产出非空类型 `T`。

### 综合示例

```yaml
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
output:
  resolution_report: { type: string }
  escalation_id: { type: string }
  assigned_operator: { type: string }
branches:
  resolved:
    description: "常规工单直接处理完毕"
    output: [resolution_report, assigned_operator]
  escalated:
    description: "紧急工单已转交专家队列"
    output: [escalation_id, assigned_operator]

graph:
  nodes:
    # 1. 用 builtin:unwrap (带 default) 将可空的 operator_override 合并为非空 string
    resolve_operator:
      use: builtin:unwrap
      with:
        input:
          source: ticket.operator_override
          default: { value: "auto_bot" }
        output:
          value: assigned_operator

    # 2. 用 builtin:switch 对 enum 字段 ticket.priority 做零开销分流
    route_priority:
      use: builtin:switch
      with:
        input:
          source: ticket.priority

    handle_normal:
      input: [ticket, assigned_operator]
      output:
        resolution_report: { type: string }
      run: "由 variables.assigned_operator 处理常规工单 variables.ticket.id，结果写入 output.resolution_report。"

    handle_urgent:
      input: [ticket, assigned_operator]
      output:
        escalation_id: { type: string }
      run: "由 variables.assigned_operator 升级紧急工单 variables.ticket.id，升级单号写入 output.escalation_id。"

  edges:
    - start -> resolve_operator
    - resolve_operator -> route_priority
    - route_priority.normal -> handle_normal
    - handle_normal -> end.resolved
    - route_priority.urgent -> handle_urgent
    - handle_urgent -> end.escalated
```

---

当我们将排他分支的一条出边连回上游已经执行过的节点时，控制流图就形成了环路。下一节介绍动态条件循环：[02. 成环回边：动态条件循环（While Loop）](./02-cycles-and-back-edges.md)。
