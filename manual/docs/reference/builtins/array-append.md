# `builtin:array_append`：列表末尾追加元素

在成环回边循环或串行流程中，零开销将单个元素 `item` 追加到列表 `source` 末尾。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空强类型列表 `{ type: array, items: T }`。 |
| **`with.input`** | `item` | **必填** | 绑定类型与 `T` 严格等价的变量/结构体字段路径，或兼容 `T` 的常量字面量。 |
| **`with.output`** | `result` | **必填** | 自动推导为 `{ type: array, items: T }`。**允许且推荐与 `source` 绑定同一个顶层列表变量名**原地更新列表。 |

### 控制流行为：
* **无分支单出边（`node -> target`）**：将 `item` 追加到 `source` 末尾生成新列表并原子写回 `result`，在下游路径上提供 `result` 的定值。

---

## 二、 YAML 用法示例

```yaml
graph:
  variables:
    attempt_history:
      type: array
      items: { type: string }
      default: []

  nodes:
    record_attempt:
      use: builtin:array_append
      with:
        input:
          source: attempt_history
          item: current_attempt_log
        output:
          result: attempt_history
```
