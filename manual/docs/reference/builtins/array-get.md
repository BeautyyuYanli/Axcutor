# `builtin:array_get`：列表按索引安全随机读取

按整数下标 `index`（支持非负正向索引与负数倒数索引，如 `-1` 表示末尾元素）在非空强类型列表 `Array<T>` 中非破坏性地读取元素。与 [`builtin:map_get`](./map-get.md) 保持完全对称的控制流设计，支持**条件分支读取（省略 `default`）**与**无分支默认值回退（传入 `default`）**两种模式。

*(注：若需从列表头部弹出首元素并获取剩余切片，请参见 [`builtin:array_shift`](./array-shift.md)。)*

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空强类型列表 `{ type: array, items: T }`（支持顶层变量或非空 `TypedDict` 的静态字段路径）。 |
| **`with.input`** | `index` | **必填** | 绑定已定值的非空 `type: integer` 变量、结构体静态字段路径或整数常量字面量（支持负数：$idx < 0$ 时按 $|A| + idx$ 归一化为倒数下标）。 |
| **`with.input`** | `default` | 可选 | 绑定已定值且满足子类型兼容 $\text{TypeOf}(\Sigma, E_{\text{def}}) <: \Sigma(v_{\text{out}})$ 的变量/字段路径，或兼容 $\Sigma(v_{\text{out}})$ 的常量字面量。 |
| **`with.output`** | `value` | **必填** | 写入当前图符号表中已声明的目标顶层变量 $v_{\text{out}}$，要求满足子类型兼容 $T <: \Sigma(v_{\text{out}})$。当省略 `default` 时，$v_{\text{out}}$ 不可与 `source` 或 `index` 的根变量同名。 |

### 控制流分支行为与反定值（Kill）语义

设源列表长度为 $m = |A|$，输入索引为 $idx \in \mathbb{Z}$，归一化下标为 $j = idx \text{ (若 } idx \ge 0 \text{)}$ 或 $j = m + idx \text{ (若 } idx < 0 \text{)}$：

* **模式 A（省略 `default`）**：暴露两个排他分支：
  * **`.some`**（运行时下标未越界 $0 \le j < m$，关联 `output: [value]`）：将命中的元素 `source[j]` 写入顶层变量 `value`，下游路径上 `value` 获得定值；
  * **`.none`**（运行时下标越界 $j < 0$ 或 $j \ge m$，关联 `output: []`，反定值杀死集 `KillVars = {value}`）：在编译期必达定值分析中从入口定值集移除 `value`（$Def_{\text{out}}(N.\text{none}) = Def_{\text{in}}(N) \setminus \{value\}$），并在运行期进入 `.none` 分支时将当前栈帧中的 `value` 显式置为未绑定（Unbound）。
* **模式 B（传入 `default`）**：**无分支单出边（`node -> target`）**。若 $0 \le j < m$ 则取 `source[j]`，否则取 `default` 写入 `value`，直接提供定值。

---

## 二、 YAML 用法示例

```yaml
graph:
  variables:
    top_candidate:
      type: string
    selected_candidate:
      type: string

  nodes:
    # 模式 A（省略 default）：按索引 0 读取首元素，暴露 .some（非空命中）与 .none（空列表越界）
    read_first_item:
      use: builtin:array_get
      with:
        input:
          source: ranked_candidates     # Array<string>
          index: 0                      # 读取首元素（传 -1 则读取最后一个元素）
        output:
          value: top_candidate          # 仅在 .some 分支下获得定值

    # 模式 B（传入 default）：按动态下标 current_idx 读取元素，越界时自动回退至 fallback_candidate
    read_with_default:
      use: builtin:array_get
      with:
        input:
          source: ranked_candidates
          index: current_idx
          default: fallback_candidate
        output:
          value: selected_candidate     # 单出边必达定值

  edges:
    - start -> read_with_default
    - read_with_default -> read_first_item
    - read_first_item.some -> evaluate_top_candidate
    - read_first_item.none -> handle_empty_candidates
```
