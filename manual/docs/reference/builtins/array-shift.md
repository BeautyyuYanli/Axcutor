# `builtin:array_shift`：列表首元素弹出与剩余切片

在成环回边（`while`）循环中，零开销从列表头部弹出第一个元素 `item` 与剩余切片列表 `rest`，用于实现**命中即停的短路早退遍历**或**动态工作队列（如 BFS）**。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空强类型列表 `{ type: array, items: T }`（支持顶层变量或结构体字段路径）。 |
| **`with.output`** | `item` | **必填** | 自动推导为列表元素类型 `T`。不可与 `rest` 同名，且**禁止在 `graph.variables` 中为 `item` 预设初值**。 |
| **`with.output`** | `rest` | **必填** | 自动推导为 `{ type: array, items: T }`。**允许且推荐与 `source` 绑定同一个顶层列表变量名**，以原地推进队列切片。 |

### 控制流分支行为：
* **`.some` 分支（运行时 `len(source) > 0`，关联 `output: [item, rest]`）**：将 `source[0]` 写入 `item`，将 `source[1:]` 写入 `rest`；下游路径上 `item` 与 `rest` 均获得定值。
* **`.empty` 分支（运行时 `len(source) == 0`，关联 `output: []`）**：不写入 `item` 与 `rest`；下游路径上 `item` 未定值（若在 `.empty` 下游误读 `item` 将在编译期报错）。

---

## 二、 YAML 用法示例

```yaml
nodes:
  pop_next_endpoint:
    use: builtin:array_shift
    with:
      input:
        source: remaining_endpoints
      output:
        item: current_endpoint
        rest: remaining_endpoints

edges:
  - start -> pop_next_endpoint
  - pop_next_endpoint.some -> try_request
  - try_request.ok -> end.success
  - try_request.failed -> pop_next_endpoint
  - pop_next_endpoint.empty -> end.all_failed
```
