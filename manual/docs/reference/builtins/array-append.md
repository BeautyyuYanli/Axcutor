# `builtin:array_append`：列表末尾追加元素

在成环回边循环或串行流程中，将单个元素 `item` 追加至列表 `source` 末尾。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空强类型列表 `{ type: array, items: T }`。 |
| **`with.input`** | `item` | **必填** | 绑定类型与 `T` 静态等价的变量/结构体字段路径，或兼容 `T` 的常量字面量。 |
| **`with.output`** | `result` | **必填** | 写入当前图符号表中已声明的目标顶层变量 $v_{\text{res}}$，要求 $\Sigma(v_{\text{res}}) \equiv \{ \texttt{type}: \texttt{array}, \texttt{items}: T \}$。可与 `source` 绑定同一个顶层列表变量名以更新变量绑定。 |

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
