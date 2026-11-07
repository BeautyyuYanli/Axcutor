# `builtin:unwrap`：可空类型拆包与默认值合并

将显式可空类型 `Optional<T>`（`type: [<BaseType>, "null"]`，含 `TypedDict` 中未列入 `required` 的可选叶子字段路径投影）收窄为非空类型 `T`。支持**条件分支拆包（省略 `default`）**与**无分支默认值合并（传入 `default`）**两种模式。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的显式可空类型 `[<BaseType>, "null"]` 变量或结构体静态字段路径（包括在 `TypedDict.properties` 中声明但未列入 `required` 的可选最末级字段，其静态投影类型自动提升为 `[<BaseType>, "null"]` 且运行期缺失键时求值为 `null`；若 `source` 推导结果本身是非空类型则编译期报错）。记移除 `"null"` 后的非空类型为 `T`。 |
| **`with.input`** | `default` | 可选 | 绑定已定值的非空 `T` 类型变量/字段路径，或兼容非空 `T` 的常量字面量（不可为 `null`）。 |
| **`with.output`** | `value` | **必填** | 自动推导为非空类型 `T`。不可与 `source` 的根变量同名。 |

### 控制流分支行为与反定值（Kill）语义

* **模式 A（省略 `default`）**：暴露两个排他分支：
  * **`.some`**（运行时 `source != null`，关联 `output: [value]`）：将非空值写入 `value`，下游路径上 `value` 已定值且类型为非空 `T`；
  * **`.none`**（运行时 `source == null`，关联 `output: []`，反定值杀死集 `KillVars = {value}`）：在编译期必达定值分析中从入口定值集移除 `value`（$Def_{\text{out}}(N.\text{none}) = Def_{\text{in}}(N) \setminus \{value\}$），并在运行期进入 `.none` 分支时将当前栈帧中的 `value` 置为未绑定（Unbound）。即使 `value` 在进入该节点前曾被赋过值，在 `.none` 下游重新定值前读取 `value` 均会在编译期报错；同时允许同一张图中多个独立的 `unwrap` 节点合法复用同一个 `value` 变量名。
* **模式 B（传入 `default`）**：**无分支单出边（`node -> target`）**。若 `source != null` 则取 `source`，否则取 `default` 写入 `value`，直接提供非空 `T` 定值。

---

## 二、 YAML 用法示例

```yaml
nodes:
  # 模式 A（省略 default）：双分支条件拆包（.some / .none）
  # 当 maybe_articles 非空时走 .some 并定值 valid_articles；为 null 时走 .none（valid_articles 在 .none 路径上被反定值置为未绑定）
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
