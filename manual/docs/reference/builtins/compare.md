# `builtin:compare`：二元比较与条件分支路由

对两个操作数执行确定性二元比较（`eq`、`ne`、`lt`、`lte`、`gt`、`gte`）。支持**直接作为条件路由节点（省略 `with.output`，暴露 `.true` / `.false` 分支）**与**将比较结果写入布尔变量（声明 `with.output.result`，无分支单出边）**两种模式。

如果需要对多个布尔条件执行逻辑与、或、非组合，请参见 [`builtin:logic`](./logic.md)；如果需要对已有布尔变量或字符串枚举执行分支路由，请参见 [`builtin:switch`](./switch.md)。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `op` | **必填** | 字符串常量字面量 `{ value: "<op>" }`，可选值为 `"eq"`、`"ne"`、`"lt"`、`"lte"`、`"gt"`、`"gte"`。 |
| **`with.input`** | `left` | **必填** | 左操作数（支持顶层变量、静态字段路径如 `doc.author_id` 或常量字面量），类型须与 `right` 满足下方对应算子的可比性规则。 |
| **`with.input`** | `right` | **必填** | 右操作数（支持顶层变量、静态字段路径如 `current_user.id` 或常量字面量），类型须与 `left` 满足下方对应算子的可比性规则。 |
| **`with.output`** | `result` | 可选 | 若声明，将布尔比较结果写入当前图中已声明的目标顶层变量（目标变量类型须为 `boolean` 或可空的 `[boolean, "null"]`）。 |

### 1. 操作数可比性规则

* **相等性比较（`op` 为 `"eq"` 或 `"ne"`）**：
  * 要求左右操作数去除 `null` 后的基础类型相互兼容（如相同类型、相同 `kind` 的资源句柄 `handle`，或 `integer` 与 `number`、兼容的结构体类型等）；
  * 支持标量、同 `kind` 的资源句柄、列表、强类型结构体与有序映射按值进行深度相等比较；
  * 支持将显式可空变量（如 `type: [string, "null"]` 或 `type: [handle, "null"]`）与 `null`（或 `{ value: null }`）直接判等。
* **序比较（`op` 为 `"lt"`、`lte"`、`"gt"` 或 `"gte"`）**：
  * 要求左右操作数均为**非空类型**（不可传入可空变量或 `null`）；
  * 仅支持以下两类序比较：
    1. **数值大小比较**：左右操作数均为数值类型（`integer` 或 `number`，允许 `integer` 与 `number` 直接比较）；
    2. **字符串字典序比较**：左右操作数均为非空字符串 `string`（按 UTF-8 / Unicode 码点字典序比较）。

### 2. 控制流分支与输出行为

* **模式 A（省略 `with.output`）**：直接暴露 **`.true`** 与 **`.false`** 两个排他分支（不产出或修改任何变量，每个分支均须在 `graph.edges` 中声明唯一一条出边）。
* **模式 B（声明 `with.output.result`）**：**无分支单出边（`node -> target`）**，将布尔比较结果 `true` / `false` 写入目标变量 `result`。

---

## 二、 YAML 用法示例

```yaml
graph:
  variables:
    is_author_match:
      type: boolean

  nodes:
    # 模式 A（省略 with.output）：直接作为数值阈值路由节点，暴露 check_score.true 与 check_score.false
    check_score:
      use: builtin:compare
      with:
        input:
          op: { value: "gte" }          # review_score >= 0.85
          left: review_score
          right: 0.85

    # 模式 B（声明 with.output.result）：将相等性判断结果写入布尔变量 is_author_match（无分支单出边）
    compute_author_match:
      use: builtin:compare
      with:
        input:
          op: { value: "eq" }           # doc.author_id == current_user.id
          left: doc.author_id
          right: current_user.id
        output:
          result: is_author_match

  edges:
    - start -> compute_author_match
    - compute_author_match -> check_score
    - check_score.true -> approve_submission
    - check_score.false -> request_revision
```
