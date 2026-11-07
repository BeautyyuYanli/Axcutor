# `builtin:map_set`：有序映射键值写入

在成环回边循环或串行流程中，向强类型有序映射（Ordered Mapping：`type: object` + `additionalProperties: TV`）插入或更新键值对，并保持 Ordered Map 的插入顺序。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空强类型有序映射 `{ type: object, additionalProperties: TV }`。 |
| **`with.input`** | `key` | **必填** | 绑定非空 `type: string` 的变量、结构体字段路径或 `{ value: "<str>" }` 常量字面量。 |
| **`with.input`** | `value` | **必填** | 绑定类型与 `TV` 静态等价的变量/结构体字段路径，或兼容 `TV` 的常量字面量。 |
| **`with.output`** | `result` | **必填** | 写入当前图符号表中已声明的目标顶层变量 $v_{\text{res}}$，要求 $\Sigma(v_{\text{res}}) \equiv \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: TV \}$。可与 `source` 绑定同一个顶层映射变量名以更新变量绑定。 |

### 控制流、纯函数式不可变更新与键序不变式

* **无分支单出边（`node -> target`）**：以纯函数式不可变方式（Copy-on-Write）构造并返回更新后的新有序映射副本写入 `result`（不原地修改输入快照引用），同时严格遵循 **`OrderedMap` 键序不变式**：
  * **键已存在（$k \in \text{dom}(source)$）**：更新该键关联的值，并**保持键 $k$ 原有的插入位置不变**；
  * **键不存在（$k \notin \text{dom}(source)$）**：将新键值对 $(k, value)$ **追加在遍历序列末尾**。

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
