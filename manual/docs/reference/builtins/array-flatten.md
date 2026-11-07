# `builtin:array_flatten`：二维列表单层保序展平

在 `for_each` 一阶集合提升或嵌套列表处理后，将非空二维列表 `Array<Array<W>>` 按外层与内层元素顺序首尾拼接展平为一维列表 `Array<W>`。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空二维列表 `{ type: array, items: { type: array, items: W } }`（外层与内层列表均不可为 `[array, "null"]`）。 |
| **`with.output`** | `result` | **必填** | 写入当前图符号表中已声明的目标顶层变量 $v_{\text{res}}$，要求 $\Sigma(v_{\text{res}}) \equiv \{ \texttt{type}: \texttt{array}, \texttt{items}: W \}$。 |

### 控制流与纯函数式不可变行为

* **无分支单出边（`node -> target`）**：以纯函数式不可变方式（Copy-on-Write）按原始顺序依次将 `source` 中的各个子列表首尾拼接构造新的一维列表副本（不原地修改输入快照引用；若外层列表为空或所有子列表均为空，则写入 `[]`）并原子写回 `result`，在下游路径上提供 `result` 的定值。

---

## 二、 YAML 用法示例

```yaml
graph:
  # 显式声明由 for_each 产出的二维标签列表 nested_tags 与展平后的一维列表 all_tags
  variables:
    nested_tags:
      type: array
      items:
        type: array
        items: { type: string }
    all_tags:
      type: array
      items: { type: string }

  nodes:
    # 步骤 1：for_each 单次迭代产出 Array<string>，外层一阶同构提升为二维列表 Array<Array<string>>
    extract_tags_per_article:
      for_each:
        source: articles
        item: article
        concurrency: 4
      input: [article]
      output:
        nested_tags:
          type: array
          items: { type: string }
      run: "提取文章 variables.article.title 的标签列表，写入 output.nested_tags。"

    # 步骤 2：调用 builtin:array_flatten 将二维列表 nested_tags 展平为一维列表 all_tags
    flatten_all_tags:
      use: builtin:array_flatten
      with:
        input:
          source: nested_tags
        output:
          result: all_tags

  edges:
    - start -> extract_tags_per_article
    - extract_tags_per_article -> flatten_all_tags
    - flatten_all_tags -> end
```
