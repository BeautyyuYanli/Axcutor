# `builtin:array_concat`：同构列表不可变拼接

将两个非空强类型列表 `left` 与 `right` 按顺序首尾拼接，构造并返回包含全部元素的新列表副本。

*(注：向列表末尾追加单个元素请参见 [`builtin:array_append`](./array-append.md)；将二维列表 `Array<Array<W>>` 展平为一维列表请参见 [`builtin:array_flatten`](./array-flatten.md)。)*

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `left` | **必填** | 绑定已定值的非空强类型列表 $T_L = \{ \texttt{type}: \texttt{array}, \texttt{items}: T_1 \}$（或 `{ value: [...] }` 数组常量字面量），须满足对目标列表类型的协变子类型兼容 $T_L <: \Sigma(v_{\text{res}})$。 |
| **`with.input`** | `right` | **必填** | 绑定已定值的非空强类型列表 $T_R = \{ \texttt{type}: \texttt{array}, \texttt{items}: T_2 \}$（或 `{ value: [...] }` 数组常量字面量），须满足对目标列表类型的协变子类型兼容 $T_R <: \Sigma(v_{\text{res}})$。 |
| **`with.output`** | `result` | **必填** | 写入当前图符号表中已声明的目标顶层变量 $v_{\text{res}}$，要求 $\Sigma(v_{\text{res}}) = \{ \texttt{type}: \texttt{array}, \texttt{items}: T_{\text{target}} \}$ 为非空列表（满足 $T_1 <: T_{\text{target}}$ 且 $T_2 <: T_{\text{target}}$）。可与 `left` 或 `right` 绑定同一个顶层列表变量名。 |

### 控制流与纯函数式不可变行为

* **无分支单出边（`node -> target`）**：以纯函数式不可变方式（Copy-on-Write）构造包含 `left` 全部元素后接 `right` 全部元素的新列表副本 $[a_0, \dots, a_{m-1}, b_0, \dots, b_{n-1}]$（不原地修改输入快照引用）并原子写回 `result`，在下游路径上提供 `result` 的定值。

---

## 二、 YAML 用法示例

```yaml
graph:
  variables:
    primary_docs:
      type: array
      items: { type: string }
    supplementary_docs:
      type: array
      items: { type: string }
    merged_docs:
      type: array
      items: { type: string }

  nodes:
    # 并发获取主检索结果与补充检索结果后，使用 builtin:array_concat 首尾拼接为 merged_docs
    concat_search_results:
      use: builtin:array_concat
      with:
        input:
          left: primary_docs
          right: supplementary_docs
        output:
          result: merged_docs

  edges:
    - start -> [fetch_primary, fetch_supplementary] -> concat_search_results
    - concat_search_results -> rerank_documents
    - rerank_documents -> end
```
