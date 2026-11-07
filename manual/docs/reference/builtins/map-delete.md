# `builtin:map_delete`：有序映射按键移除

按动态字符串键 `key` 从非空强类型有序映射（Ordered Mapping：`type: object` 且声明了 `additionalProperties` 值类型）中移除指定条目，并以纯函数式不可变方式返回保留原键序的新映射副本。向有序映射写入键值对请参见 [`builtin:map_set`](./map-set.md)；按动态键读取有序映射请参见 [`builtin:map_get`](./map-get.md)。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空强类型有序映射（`type: object` 且声明了统一的 `additionalProperties` 值类型）。 |
| **`with.input`** | `key` | **必填** | 绑定已定值的非空字符串（`type: string`），可为顶层变量、结构体静态字段路径或 `{ value: "<str>" }` 常量字面量。 |
| **`with.output`** | `result` | **必填** | 写入当前图中已声明的目标顶层变量，要求 `source` 的映射类型与该目标变量的声明类型兼容（为其子类型或相同类型）。可与 `source` 绑定同一个顶层映射变量名以更新变量绑定。 |

### 控制流、纯函数式不可变删除与键序保持

* **无分支单出边（`node -> target`）**：以纯函数式不可变方式（Copy-on-Write）构造并返回移除键 `key` 后的新有序映射副本写入 `result`（不原地修改输入快照引用）：
  * **键已存在于 `source` 中**：移除 `key` 对应的键值对，并**保持其余所有键原有的相对插入顺序不变**；
  * **键不存在于 `source` 中**：原样返回 `source` 的浅拷贝副本（不产生异常）。

---

## 二、 YAML 用法示例

```yaml
nodes:
  # 从活跃会话映射 active_sessions 中移除已过期的 expired_token，并将新映射副本写回 active_sessions
  evict_expired_session:
    use: builtin:map_delete
    with:
      input:
        source: active_sessions        # OrderedMap<string, SessionInfo>
        key: expired_token             # 待移除的键（type: string）
      output:
        result: active_sessions        # 写回更新后的有序映射副本

edges:
  - start -> evict_expired_session
  - evict_expired_session -> end
```
