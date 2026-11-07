# `builtin:math`：除零安全二元算术运算

对非空数值（`integer` 或 `number`）执行确定性二元算术运算（`add`、`sub`、`mul`、`div`、`mod`）。为保障“所有 `builtin:*` 算子永不抛出可恢复运行时异常”的核心不变式，`builtin:math` 在编译期将永不失败的 `add` / `sub` / `mul` 与可能发生除零的 `div` / `mod` 在静态控制分支契约上显式区分。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `op` | **必填** | 字符串常量字面量 `{ value: "<op>" }`，取值属于 `{"add", "sub", "mul", "div", "mod"}`。 |
| **`with.input`** | `left` | **必填** | 左操作数（非空 `integer` 或 `number` 变量、结构体静态字段路径或数值字面量；当 `op` 为 `"mod"` 时必须为非空 `integer`）。 |
| **`with.input`** | `right` | **必填** | 右操作数（非空 `integer` 或 `number` 变量、结构体静态字段路径或数值字面量；当 `op` 为 `"mod"` 时必须为非空 `integer`）。 |
| **`with.input`** | `default` | 可选（仅限 `div` / `mod`） | **仅当 `op` 为 `"div"` 或 `"mod"` 时允许声明**（`add` / `sub` / `mul` 禁止声明 `default`）。若声明，须满足子类型兼容 $\text{TypeOf}(\Sigma, E_{\text{def}}) <: \Sigma(v_{\text{res}})$ 或字面量兼容 $L_{\text{def}} \models \Sigma(v_{\text{res}})$。 |
| **`with.output`** | `result` | **必填** | 写入当前图符号表中已声明的目标顶层变量 $v_{\text{res}}$，要求结果数值类型 $T_{\text{math}}$ 满足 $T_{\text{math}} <: \Sigma(v_{\text{res}})$。 |

### 1. 结果数值类型 $T_{\text{math}}$ 推导规则

* **整数闭合运算**：当 `op` $\in \{\texttt{"add"}, \texttt{"sub"}, \texttt{"mul"}, \texttt{"mod"}\}$ 且 `left`、`right` 两侧均为 `integer` 时，$T_{\text{math}} = \{ \texttt{type}: \texttt{integer} \}$（由于 `integer <: number`，该结果既可写入 `integer` 变量，也可写入 `number` 变量）。
* **浮点运算与真除法**：当 `op` $= \texttt{"div"}$（浮点真除法）或任一操作数为 `number` 时，$T_{\text{math}} = \{ \texttt{type}: \texttt{number} \}$。

### 2. 控制流分支行为与反定值（Kill）语义

* **情况 A（`op` 为 `"add"` / `"sub"` / `"mul"`，或 `op` 为 `"div"` / `"mod"` 且传入了 `default`）**：
  * **无分支单出边（`node -> target`）**：直接将计算结果（或除数为 `0` 时的 `default` 回退值）写入 `result`。允许 `result` 与 `left` 或 `right` 绑定同一个顶层变量名（如 `step_index = step_index + 1`）。
* **情况 B（`op` 为 `"div"` 或 `"mod"` 且省略了 `default`）**：
  * 要求输出变量 $v_{\text{res}}$ 不可与 `left` 或 `right` 的根变量同名；
  * 暴露两个排他分支：
    * **`.some`**（运行时除数 `right != 0`，关联 `output: [result]`）：将商或余数写入 `result`，下游路径上 `result` 获得定值；
    * **`.none`**（运行时除数 `right == 0`，关联 `output: []`，反定值杀死集 `KillVars = {result}`）：在编译期必达定值分析中从入口定值集移除 `result`（$Def_{\text{out}}(N.\text{none}) = Def_{\text{in}}(N) \setminus \{result\}$），并在运行期进入 `.none` 分支时将当前栈帧中的 `result` 显式置为未绑定（Unbound）。

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
    # 示例 1：计数器原地自增（add 永不失败，无分支单出边，result 可与 left 同名）
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
