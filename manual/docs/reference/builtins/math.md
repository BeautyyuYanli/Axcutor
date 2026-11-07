# `builtin:math`：除零安全二元算术运算

对非空数值（`integer` 或 `number`）执行确定性二元算术运算（`add`、`sub`、`mul`、`div`、`mod`）。`add`、`sub`、`mul` 不会发生除零错误，始终采用无分支单出边；可能发生除零的 `div` 与 `mod` 则根据是否提供 `default` 回退值，在编译期区分无分支模式与除零安全双分支模式（`.some` / `.none`）。

---

## 一、 参数与分支规则

| 方向 | 参数名 | 必填/可选 | 类型与约束说明 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `op` | **必填** | 字符串常量字面量 `{ value: "<op>" }`，可选值为 `"add"`、`"sub"`、`"mul"`、`"div"`、`"mod"`。 |
| **`with.input`** | `left` | **必填** | 左操作数：非空 `integer` 或 `number` 变量、结构体静态字段路径或数值字面量；当 `op` 为 `"mod"` 时必须为非空 `integer`。 |
| **`with.input`** | `right` | **必填** | 右操作数：非空 `integer` 或 `number` 变量、结构体静态字段路径或数值字面量；当 `op` 为 `"mod"` 时必须为非空 `integer`。 |
| **`with.input`** | `default` | 可选（仅限 `div` / `mod`） | **仅当 `op` 为 `"div"` 或 `"mod"` 时可声明**（`add` / `sub` / `mul` 禁止声明 `default`）。声明时，其类型或字面量值须与 `result` 目标变量的类型兼容。 |
| **`with.output`** | `result` | **必填** | 接收运算结果的目标顶层变量（须已在当前图的 `variables` 中声明），其声明类型须与运算结果数值类型兼容。 |

### 1. 结果数值类型规则

* **整数运算**：当 `op` 为 `"add"`、`"sub"`、`"mul"` 或 `"mod"`，且 `left`、`right` 均为 `integer` 时，产出 `integer`（由于 `integer` 可兼容赋给 `number`，该结果既可写入 `integer` 变量，也可写入 `number` 变量）。
* **浮点运算与除法**：当 `op` 为 `"div"`，或任一操作数为 `number` 时，产出 `number`（目标变量 `result` 须为 `number` 类型）。

### 2. 无分支模式 vs. 除零安全双分支模式（`.some` / `.none`）

* **无分支模式（`op` 为 `"add"` / `"sub"` / `"mul"`，或 `op` 为 `"div"` / `"mod"` 且提供了 `default`）**：
  * **单出边（`node -> target`）**：直接将计算结果（或除数为 `0` 时的 `default` 回退值）写入 `result`。允许 `result` 与 `left` 或 `right` 绑定同一个顶层变量（如自增更新 `step_index`）。
* **除零安全双分支模式（`op` 为 `"div"` 或 `"mod"` 且未提供 `default`）**：
  * 输出变量 `result` 不可与 `left` 或 `right` 的根变量同名；
  * 节点暴露两个排他分支：
    * **`.some`**（运行时除数 `right != 0`）：将商或余数写入 `result`，下游分支可安全读取 `result`；
    * **`.none`**（运行时除数 `right == 0`）：不产出 `result`；进入 `.none` 分支时，`result` 会被置为未绑定状态（即使此前已赋值也不可在该分支下游直接读取，须重新赋值后方可使用）。

---

## 二、 YAML 用法示例

```yaml
graph:
  variables:
    step_index:
      type: integer
      default: 0
    avg_score:
      type: number
    unit_price:
      type: number

  nodes:
    # 示例 1：计数器自增并更新同名变量 step_index（add 永不失败，无分支单出边，result 可与 left 同名）
    increment_step:
      use: builtin:math
      with:
        input:
          op: { value: "add" }
          left: step_index
          right: 1
        output:
          result: step_index

    # 示例 2：带 default 的安全除法（当 item_count 为 0 时自动回退为 0.0，无分支单出边）
    compute_avg_with_default:
      use: builtin:math
      with:
        input:
          op: { value: "div" }
          left: total_score
          right: item_count
          default: 0.0
        output:
          result: avg_score

    # 示例 3：不传 default 的条件除法（暴露 .some 与除零分支 .none）
    compute_unit_price:
      use: builtin:math
      with:
        input:
          op: { value: "div" }
          left: total_amount
          right: quantity
        output:
          result: unit_price

  edges:
    - start -> increment_step
    - increment_step -> compute_avg_with_default
    - compute_avg_with_default -> compute_unit_price
    - compute_unit_price.some -> record_valid_price
    - compute_unit_price.none -> handle_zero_quantity
```
