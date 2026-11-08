# `builtin:switch`：布尔与枚举确定性路由

对已定值的非空 `boolean` 或带 `enum` 约束的非空 `string`（支持顶层变量名或非空 `TypedDict` 的静态字段路径）执行零开销排他分支路由。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空 `type: boolean`，或带非空 `enum: [<v1>, <v2>, ...]` 约束的非空 `type: string`。当为 `string` + `enum` 时，每个枚举值 `<vi>` 必须是符合[统一标识符规则](../../../spec/01-grammar-and-ast/01-document-and-node-grammar.md)的 `BranchId`，排除 `start`、`end`、`fail` 及全部 35 个 Python 硬关键字，否则编译期报错。 |
| **`with.output`** | *无* | *禁止声明* | 仅执行控制流路由，不产出或修改任何变量。 |

### 控制流分支行为（编译期穷尽性检查）

* **当 `source` 为 `type: boolean` 时**：固定暴露 **`.true`** 与 **`.false`** 两个分支。
* **当 `source` 为 `type: string, enum: [<v1>, <v2>, ...]` 时**：在校验所有 `<vi>` 均为合法 `BranchId` 后，自动暴露与每个枚举值字面量同名的 **`.<v1>`**、**`.<v2>`**... 分支。每一个枚举分支均必须在 `graph.edges` 中声明唯一一条出边。

---

## 二、 YAML 用法示例

```yaml
nodes:
  # 形态 A：布尔二路路由（暴露 check_review_passed.true 与 check_review_passed.false）
  check_review_passed:
    use: builtin:switch
    with:
      input:
        source: review_result.passed

  # 形态 B：枚举多路穷尽路由（若 priority 的 enum 为 [low, medium, high]，暴露对应三个分支）
  # enum 元素遵循统一 BranchId 规则：可用 low / medium / high 或软关键字 match。
  # 不可使用连字符、start / end / fail 或 Python 硬关键字（如 class）。
  route_by_priority:
    use: builtin:switch
    with:
      input:
        source: ticket.priority

edges:
  - start -> check_review_passed
  - check_review_passed.true -> route_by_priority
  - check_review_passed.false -> handle_rejection
  - route_by_priority.high -> urgent_handler
  - route_by_priority.medium -> standard_handler
  - route_by_priority.low -> batch_queue
```
