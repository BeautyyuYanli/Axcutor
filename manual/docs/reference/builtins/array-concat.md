# `builtin:array_concat`：同构列表不可变拼接

将两个非空强类型列表 `left` 与 `right` 按顺序首尾拼接，构造并返回包含全部元素的新列表副本。

若需向列表末尾追加单个元素，请参见 [`builtin:array_append`](./array-append.md)；若需将二维嵌套列表展平为一维列表，请参见 [`builtin:array_flatten`](./array-flatten.md)。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `left` | **必填** | 绑定已定值的非空强类型列表变量（`type: array` 且显式声明 `items` 元素类型），或传入 `{ value: [...] }` 数组常量字面量；其元素类型须与 `result` 目标列表的元素类型兼容（相同或为其子类型）。 |
| **`with.input`** | `right` | **必填** | 绑定已定值的非空强类型列表变量（`type: array` 且显式声明 `items` 元素类型），或传入 `{ value: [...] }` 数组常量字面量；其元素类型须与 `result` 目标列表的元素类型兼容（相同或为其子类型）。 |
| **`with.output`** | `result` | **必填** | 写入当前图中已声明的非空强类型顶层列表变量（`type: array` 且显式声明 `items` 元素类型），其元素类型须同时兼容 `left` 与 `right` 的元素类型。可与 `left` 或 `right` 绑定同一个顶层列表变量名。 |

### 控制流与纯函数式不可变行为

* **无分支单出边（`node -> target`）**：以纯函数式不可变方式（Copy-on-Write）按顺序构造包含 `left` 全部元素后接 `right` 全部元素的新列表副本（不原地修改输入列表引用），并原子写回 `result`，在下游路径上提供 `result` 的定值。

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
