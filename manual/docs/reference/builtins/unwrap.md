# `builtin:unwrap`：可空类型拆包与默认值合并

将显式可空类型 `Optional<T>`（`type: [<BaseType>, "null"]`）安全收窄为纯正的非空类型 `T`。支持**条件分支拆包（省略 `default`）**与**无分支默认值合并（传入 `default`）**两种模式。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的显式可空类型 `[<BaseType>, "null"]` 变量或结构体静态字段路径（若源本身已是非空类型则编译期报错）。记移除 `"null"` 后的非空类型为 `T`。 |
| **`with.input`** | `default` | 可选 | 绑定已定值的非空 `T` 类型变量/字段路径，或符合非空 `T` 的常量字面量（严禁为 `null`）。 |
| **`with.output`** | `value` | **必填** | 自动推导为非空类型 `T`。**不可与 `source` 的根变量同名**；且在省略 `default`（模式 A）时，禁止在 `graph.variables` 中为 `value` 预设初值。 |

### 控制流分支行为：
* **模式 A（省略 `default`）**：暴露两个排他分支：
  * **`.some`**（运行时 `source != null`，关联 `output: [value]`）：将非空值写入 `value`，下游路径上 `value` 已定值且类型为非空 `T`；
  * **`.none`**（运行时 `source == null`，关联 `output: []`）：不写入 `value`，下游路径上 `value` 未定值。
* **模式 B（传入 `default`）**：**无分支单出边（`node -> target`）**。若 `source != null` 则取 `source`，否则取 `default` 写入 `value`，直接提供非空 `T` 定值。

---

## 二、 YAML 用法示例

```yaml
nodes:
  # 模式 A（未传 default）：双分支条件拆包（.some / .none）
  unwrap_optional_articles:
    use: builtin:unwrap
    with:
      input:
        source: maybe_articles
      output:
        value: valid_articles

  # 模式 B（传入 default）：无分支默认值合并（doc_meta.maybe_author ?? "佚名"）
  resolve_author_name:
    use: builtin:unwrap
    with:
      input:
        source: doc_meta.maybe_author
        default: { value: "佚名" }
      output:
        value: author_name

edges:
  - start -> resolve_author_name
  - resolve_author_name -> unwrap_optional_articles
  - unwrap_optional_articles.some -> process_articles
  - unwrap_optional_articles.none -> end.skipped
```
