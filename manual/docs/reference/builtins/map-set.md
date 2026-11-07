# `builtin:map_set`：有序映射键值写入

在成环回边循环或串行流程中，向强类型有序映射（`OrderedMap`：`type: object` + `additionalProperties: <值类型>`）插入或更新键值对，并保持 `OrderedMap` 的插入顺序。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空强类型有序映射（`type: object` 且声明了 `additionalProperties`），其值类型须与目标变量 `result` 声明的值类型兼容（相同或为其子类型）。 |
| **`with.input`** | `key` | **必填** | 绑定非空字符串（`type: string`）类型的变量、结构体字段路径，或 `{ value: "<str>" }` 字符串常量字面量。 |
| **`with.input`** | `value` | **必填** | 绑定与目标映射值类型（`result` 的 `additionalProperties` 类型）兼容的变量、结构体字段路径或常量字面量。 |
| **`with.output`** | `result` | **必填** | 写入当前图 `variables` 中已声明的目标顶层变量，要求其类型为非空有序映射（`type: object` 且声明了 `additionalProperties`）。可与 `source` 绑定同一个顶层映射变量名，以写时复制方式更新该变量。 |

### 控制流、写时复制更新与键序保持规则

* **无分支单出边（`node -> target`）**：以写时复制（Copy-on-Write）的不可变方式构造并返回更新后的新 `OrderedMap` 副本写入 `result`（不原地修改 `source` 原始映射），同时严格遵循 **`OrderedMap` 键序保持规则**：
  * **已有键（`key` 已存在于 `source` 中）**：**原位更新**该键关联的值，保持该键原有的插入位置与遍历顺序不变；
  * **新键（`key` 不存在于 `source` 中）**：将新键值对 **`key: value` 追加在末尾**。

---

## 二、 YAML 用法示例

```yaml
graph:
  # 显式声明带类型的空有序映射初值
  variables:
    result_map:
      type: object
      additionalProperties: { type: string }
      default: {}

  nodes:
    # 将键值对 current_task.id -> current_output 写入 result_map（新键追加至末尾，已有键保持原位更新）
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
