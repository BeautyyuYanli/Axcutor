# `builtin:map_get`：有序映射动态键查找与默认值回退

按动态字符串键 `key` 在非空强类型有序映射（Ordered Mapping：`type: object` + `additionalProperties: TV`）中查找对应的值。与 [`builtin:unwrap`](./unwrap.md) 保持一致的控制流体验，支持**条件分支查找（省略 `default`）**与**无分支默认值回退（传入 `default`）**两种模式。

*(注：向有序映射写入键值对请参见 [`builtin:map_set`](./map-set.md)；从有序映射中按键移除条目请参见 [`builtin:map_delete`](./map-delete.md)。)*

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空强类型有序映射 `{ type: object, additionalProperties: TV }`（支持顶层变量或非空 `TypedDict` 的静态字段路径）。 |
| **`with.input`** | `key` | **必填** | 绑定已定值的非空 `type: string` 变量、结构体静态字段路径或 `{ value: "<str>" }` 常量字面量。 |
| **`with.input`** | `default` | 可选 | 绑定已定值且类型与 `TV` 静态等价的变量/字段路径，或兼容 `TV` 的常量字面量。 |
| **`with.output`** | `value` | **必填** | 自动推导为映射值类型 `TV`。不可与 `source` 的根变量同名。 |

### 控制流分支行为与反定值（Kill）语义

* **模式 A（省略 `default`）**：暴露两个排他分支：
  * **`.some`**（运行时 `key in source`，关联 `output: [value]`）：将查找到的值 `source[key]` 写入顶层变量 `value`，下游路径上 `value` 获得定值且类型为 `TV`；
  * **`.none`**（运行时 `key not in source`，关联 `output: []`，反定值杀死集 `KillVars = {value}`）：在编译期必达定值分析中从入口定值集移除 `value`（$Def_{\text{out}}(N.\text{none}) = Def_{\text{in}}(N) \setminus \{value\}$），并在运行期进入 `.none` 分支时将当前栈帧中的 `value` 显式置为未绑定（Unbound）。即使 `value` 在进入该节点前曾被赋过值，在 `.none` 下游重新定值前读取 `value` 均会在编译期报错；同时允许同一张图中多个独立的 `map_get` 节点合法复用同一个 `value` 变量名。
* **模式 B（传入 `default`）**：**无分支单出边（`node -> target`）**。若 `key in source` 则取 `source[key]`，否则取 `default` 写入 `value`，直接提供 `TV` 类型定值。

---

## 二、 YAML 用法示例

```yaml
nodes:
  # 模式 A（省略 default）：双分支条件查找（.some / .none）
  # 若 result_cache 中存在键 current_query_id 则走 .some 并定值 cached_answer；不存在则走 .none 回源计算
  lookup_cache:
    use: builtin:map_get
    with:
      input:
        source: result_cache           # OrderedMap<string, string>
        key: current_query_id          # 动态查找键（type: string）
      output:
        value: cached_answer           # 仅在 .some 分支下获得定值

  # 模式 B（传入 default）：无分支默认值回退
  # 若 score_map 中不存在键 module_name，自动回退为默认值 0.0 并写入 module_score
  lookup_score_with_fallback:
    use: builtin:map_get
    with:
      input:
        source: score_map              # OrderedMap<string, number>
        key: module_name
        default: 0.0
      output:
        value: module_score            # 单出边必达定值（type: number）

edges:
  - start -> lookup_score_with_fallback
  - lookup_score_with_fallback -> lookup_cache
  # 命中缓存：直接使用 cached_answer
  - lookup_cache.some -> end
  # 未命中缓存：执行回源节点计算 cached_answer 后汇合至 end
  - lookup_cache.none -> compute_fresh_answer
  - compute_fresh_answer -> end
```
