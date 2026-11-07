# `builtin:unwrap`：可空类型拆包与默认值合并

将显式可空类型 `Optional<T>`（`type: [<BaseType>, "null"]`，含 `TypedDict` 中未列入 `required` 的可选叶子字段路径投影）收窄为非空目标类型。支持**条件分支拆包（省略 `default`）**与**无分支默认值合并（传入 `default`）**两种模式。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的显式可空类型 `[<BaseType>, "null"]` 变量或结构体静态字段路径（包括在 `TypedDict.properties` 中声明但未列入 `required` 的可选最末级字段，其静态投影类型自动视为 `[<BaseType>, "null"]`，运行期缺失键时求值为 `null`）。若 `source` 本身是非空类型，则在编译期报错。 |
| **`with.input`** | `default` | 可选 | 绑定已定值且类型兼容目标变量 `value` 的变量/字段路径，或兼容目标类型的非空常量字面量（不可为 `null`）。 |
| **`with.output`** | `value` | **必填** | 写入当前图 `variables` 中已声明的目标顶层变量。要求目标变量本身声明为非空类型、兼容 `source` 去掉 `"null"` 后的非空内层类型 `<BaseType>`，且不可与 `source` 的根变量同名。 |

### 控制流分支与定值行为

* **模式 A（省略 `default`）**：暴露两个排他分支：
  * **`.some`**（运行时 `source != null`，关联 `output: [value]`）：将非空值写入 `value`，下游路径上 `value` 已定值且为非空类型；
  * **`.none`**（运行时 `source == null`，关联 `output: []`）：将 `value` 重置为未定值（未绑定）状态。即使 `value` 在进入该节点前曾被赋过值，在 `.none` 下游重新定值前读取 `value` 均会在编译期报错；同时允许同一张图中多个独立的 `unwrap` 节点复用同一个 `value` 变量名。
* **模式 B（传入 `default`）**：**无分支单出边（`node -> target`）**。若 `source != null` 则取 `source`，否则取 `default` 写入 `value`，直接向后续节点提供非空定值。

---

## 二、 YAML 用法示例

```yaml
graph:
  # 显式声明拆包后的非空目标变量类型
  variables:
    valid_articles:
      type: array
      items: { type: string }
    author_name:
      type: string

  nodes:
    # 模式 A（省略 default）：双分支条件拆包（.some / .none）
    # 当 maybe_articles 非空时走 .some 并定值 valid_articles；为 null 时走 .none（valid_articles 在 .none 路径上重置为未定值状态）
    unwrap_optional_articles:
      use: builtin:unwrap
      with:
        input:
          source: maybe_articles
        output:
          value: valid_articles

    # 模式 B（传入 default）：无分支默认值合并（doc_meta.maybe_author ?? "佚名"）
    # doc_meta.maybe_author 既可显式声明为 [string, "null"]，也可为未列入 doc_meta.required 的可选 string 属性
    # 直接产出非空 string 变量 author_name
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
