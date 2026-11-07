# `builtin:array_get`：列表按索引安全随机读取

按整数下标 `index`（支持正向索引与负数倒数索引，如 `-1` 表示末尾元素）在非空强类型列表 `Array<T>` 中非破坏性地读取元素。与 [`builtin:map_get`](./map-get.md) 保持完全对称的控制流设计，支持**条件分支读取（省略 `default`）**与**无分支默认值回退（传入 `default`）**两种模式。

*(注：若需从列表头部弹出首元素并获取剩余切片，请参见 [`builtin:array_shift`](./array-shift.md)。)*

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空强类型列表 `{ type: array, items: T }`（支持顶层变量或非空 `TypedDict` 的静态字段路径）。 |
| **`with.input`** | `index` | **必填** | 绑定已定值的非空 `type: integer` 变量、结构体静态字段路径或整数常量字面量。支持从 `0` 开始的正向索引，以及负数倒数索引（如 `-1` 表示末尾元素，`-2` 表示倒数第二个元素）。 |
| **`with.input`** | `default` | 可选 | 绑定已定值的变量、结构体静态字段路径或常量字面量，其类型必须兼容输出目标变量 `value` 的声明类型。 |
| **`with.output`** | `value` | **必填** | 写入当前图中已声明的目标顶层变量，列表元素类型 `T` 必须兼容该目标变量的声明类型。当省略 `default` 时，目标变量不可与 `source` 或 `index` 的根变量同名。 |

### 索引规则与控制流分支行为

**索引规则**：
* **正向索引**：从 `0` 起算，`0` 表示首元素，有效范围为 `0` 至 `列表长度 - 1`。
* **负数倒数索引**：从列表末尾倒数起算，`-1` 表示最后一个元素，有效范围为 `-列表长度` 至 `-1`（等价于 `列表长度 + index`）。超出上述范围的索引均视为越界（空列表下任意索引均越界）。

**控制流模式**：
* **模式 A（省略 `default`）**：节点暴露两个互斥分支：
  * **`.some`（索引命中）**：当 `index` 在有效范围内时走此分支，将读取到的列表元素写入输出变量 `value`，下游节点可安全读取 `value`。
  * **`.none`（索引越界）**：当 `index` 超出有效范围时走此分支。即使输出变量 `value` 在进入本节点前已赋值，进入 `.none` 分支后也会被重置为**未定值（未绑定）**状态，下游在重新赋值前不可直接读取 `value`。
* **模式 B（传入 `default`）**：**无分支单出边（`node -> target`）**。当 `index` 在有效范围内时将对应元素写入 `value`，越界时自动将 `default` 的值写入 `value`，下游路径上 `value` 始终保持已定值状态。

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
