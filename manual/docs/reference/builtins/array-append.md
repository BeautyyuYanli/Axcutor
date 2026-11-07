# `builtin:array_append`：列表末尾追加元素

在成环回边循环或串行流程中，将单个元素 `item` 追加至列表 `source` 末尾。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空强类型列表，且其元素类型与 `result` 目标列表的元素类型兼容（支持子类型协变兼容）。 |
| **`with.input`** | `item` | **必填** | 绑定兼容 `result` 目标列表元素类型的变量、结构体字段路径或常量字面量（如向 `number` 列表追加 `integer`，或向允许可空字符串的列表追加非空 `string`）。 |
| **`with.output`** | `result` | **必填** | 写入当前图中已声明的目标顶层列表变量（要求声明为非空 `array` 类型并指定 `items` 元素类型）。可与 `source` 绑定同一个顶层列表变量名以更新列表绑定。 |

### 控制流与纯函数式不可变行为

* **无分支单出边（`node -> target`）**：以纯函数式不可变方式（Copy-on-Write）将 `item` 追加至 `source` 末尾构造新列表副本（不原地修改输入快照引用）并原子写回 `result`，在下游路径上提供 `result` 的定值。

---

## 二、 YAML 用法示例

```yaml
graph:
  # 显式声明带类型的空数组初值
  variables:
    attempt_history:
      type: array
      items: { type: string }
      default: []

  nodes:
    # 将单步产出的 current_attempt_log 追加至 attempt_history 末尾并写回新列表副本
    record_attempt:
      use: builtin:array_append
      with:
        input:
          source: attempt_history
          item: current_attempt_log
        output:
          result: attempt_history
```
