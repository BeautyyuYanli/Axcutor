# `builtin:logic`：布尔逻辑组合与取反

对非空布尔值执行确定性逻辑运算（`and`、`or`、`not`）。与 [`builtin:compare`](./compare.md) 保持一致的控制流契约，支持**直接暴露 `.true` / `.false` 分支（省略 `with.output`）**与**输出布尔变量（声明 `with.output.result`，无分支单出边）**两种模式。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `op` | **必填** | 字符串常量字面量 `{ value: "<op>" }`，取值属于 `{"and", "or", "not"}`。 |
| **`with.input`** | `left` | 当 `op` 为 `"and"` / `"or"` 时**必填** | 绑定已定值的非空 `type: boolean` 变量、结构体静态字段路径或布尔常量字面量（当 `op` 为 `"not"` 时**禁止声明**）。 |
| **`with.input`** | `right` | 当 `op` 为 `"and"` / `"or"` 时**必填** | 绑定已定值的非空 `type: boolean` 变量、结构体静态字段路径或布尔常量字面量（当 `op` 为 `"not"` 时**禁止声明**）。 |
| **`with.input`** | `source` | 当 `op` 为 `"not"` 时**必填** | 绑定已定值的非空 `type: boolean` 变量、结构体静态字段路径或布尔常量字面量（当 `op` 为 `"and"` / `"or"` 时**禁止声明**）。 |
| **`with.output`** | `result` | 可选 | 若声明，写入当前图符号表中已声明的目标顶层变量 $v_{\text{res}}$，要求满足 $\{ \texttt{type}: \texttt{boolean} \} <: \Sigma(v_{\text{res}})$。 |

### 控制流分支与输出行为

* **模式 A（省略 `with.output`）**：直接暴露 **`.true`** 与 **`.false`** 两个排他分支（不修改任何变量）。
* **模式 B（声明 `with.output.result`）**：**无分支单出边（`node -> target`）**，将逻辑运算结果写入目标变量 `result`（当 `op` 为 `"not"` 时，允许 `result` 与 `source` 绑定同一个顶层布尔变量名以实现原地取反）。

---

## 二、 YAML 用法示例

```yaml
graph:
  variables:
    should_commit:
      type: boolean

  nodes:
    # 示例 1：逻辑非（op 为 "not" 时传入 source，声明 with.output.result 输出布尔变量）
    invert_dry_run_flag:
      use: builtin:logic
      with:
        input:
          op: { value: "not" }
          source: request.is_dry_run
        output:
          result: should_commit

    # 示例 2：逻辑与（op 为 "and" 时传入 left 与 right，省略 with.output 直接暴露 .true / .false 分支）
    check_can_execute:
      use: builtin:logic
      with:
        input:
          op: { value: "and" }
          left: is_verified
          right: should_commit

  edges:
    - start -> invert_dry_run_flag
    - invert_dry_run_flag -> check_can_execute
    - check_can_execute.true -> commit_transaction
    - check_can_execute.false -> skip_execution
```
