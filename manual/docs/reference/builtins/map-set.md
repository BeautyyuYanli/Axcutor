# `builtin:map_set`：有序映射键值写入

在成环回边循环或串行流程中，零开销向强类型有序映射（Ordered Mapping：`type: object` + `additionalProperties: TV`）插入或更新键值对，并严格保持 Ordered Map 的插入顺序。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空强类型有序映射 `{ type: object, additionalProperties: TV }`。 |
| **`with.input`** | `key` | **必填** | 绑定非空 `type: string` 的变量、结构体字段路径或 `{ value: "<str>" }` 常量字面量。 |
| **`with.input`** | `value` | **必填** | 绑定类型与 `TV` 严格等价的变量/结构体字段路径，或兼容 `TV` 的常量字面量。 |
| **`with.output`** | `result` | **必填** | 自动推导为 `{ type: object, additionalProperties: TV }`。**允许且推荐与 `source` 绑定同一个顶层映射变量名**原地更新。 |

### 控制流与运行时行为：
* **无分支单出边（`node -> target`）**：若 `key` 在 `source` 中不存在则追加至末尾；若 `key` 已存在则原地更新其对应的值并保持原键位置不变。原子写回 `result` 并提供定值。

---

## 二、 YAML 用法示例

```yaml
graph:
  variables:
    result_map:
      type: object
      additionalProperties: { type: string }
      default: {}

  nodes:
    save_task_result:
      use: builtin:map_set
      with:
        input:
          source: result_map
          key: current_task.id
          value: current_output
        output:
          result: result_map
```
