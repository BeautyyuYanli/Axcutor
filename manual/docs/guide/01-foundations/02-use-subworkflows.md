# 02. Use：跨文件调用子工作流

在 Axcutor 中，任何独立的工作流文件都可以被其他工作流作为子节点调用。

当一个图内节点声明 `use: workflow:<path>`（或调用运行时内建算子 `use: builtin:<op>`）时，运行时会为其建立一个**完全隔离的函数调用栈帧（Function Frame）**。被调用的子工作流拥有独立的 `input`、`output` 和 `branches` 命名空间，无法隐式访问父图中的任何变量，严格通过 **`with`** 进行按值传参与返回值写回。

---

## 一、 `use` 与 `with` 的四种传参方式

调用节点通过可选的 `with.input` 将当前图的数据绑定给子工作流的入参，并通过 `with.output` 将子工作流的产出映射回当前图的 `variables`：

```yaml
nodes:
  normalize_doc:
    use: workflow:text/normalize_document
    with:
      # 输入绑定：<子工作流 input 参数名>: <当前图变量名 | 静态字段路径 | 非字符串标量字面量 | { value: 任意字面量 }>
      input:
        raw_text: user_submission.body     # 1. 绑定非空 TypedDict 结构体的静态字段路径
        author_id: current_user_id         # 2. 绑定当前图中的顶层变量名
        max_length: 2000                   # 3. 直接传递非字符串常量字面量（integer / number / boolean / null）
        format_style: { value: "strict" }  # 4. 使用 { value: ... } 显式传递字符串或复合常量字面量
      # 输出适配：<子工作流 output 字段名>: <写回当前图 variables 的目标变量名>
      output:
        clean_text: normalized_body
        word_count: doc_word_count
```

### 1. 默认同名匹配（省略 `with.input` 或 `with.output`）
* 若省略 `with.input` 中的某个参数（或完全省略 `with.input`），运行时会自动在当前作用域中查找**与子工作流 `input` 参数同名**的顶层变量传入，并在编译期验证其类型与定值状态。
* 若省略 `with.output` 中的某个字段（或完全省略 `with.output`），子工作流的 `output` 字段默认按**原字段名**写回当前图的 `variables`。

### 2. 变量名与结构体静态字段路径绑定
* 当 `with.input.<param>` 的值为裸字符串时，它始终代表**引用当前作用域中的变量**：
  * `current_user_id`：引用当前图顶层变量 `variables.current_user_id`；
  * `user_submission.body`：对非空强类型结构体 `user_submission` 执行静态字段路径投影，零开销提取其内部的 `body` 字段传入。

### 3. 常量字面量传递（无需声明临时图变量）
* **非字符串标量字面量**：数值（`integer` / `number`）、布尔值（`boolean`）或 `null` 可以直接写在 `with.input.<param>` 下作为常量传入（如 `max_length: 2000`、`strict_mode: true`）。
* **字符串与复合常量包装器 `{ value: <literal> }`**：因为裸字符串代表变量引用，如果要传递**字符串字面量**（如 `"strict"`）、**数组常量**或**对象常量**，统一使用 `{ value: <literal> }` 包装。

---

## 二、 子工作流控制分支的透明继承

如果被调用的子工作流在顶层声明了 `branches`（例如 `approved` 与 `rejected`），那么调用它的 `use` 节点在当前图中会自动暴露完全相同的控制分支。你可以直接在当前图的 `edges` 中通过 `<use_node>.<branch> -> target` 进行连边路由。

---

除了跨文件调用外部工作流，我们经常还需要在**同一个工作流文件内部**把若干步骤封装为一个块级子流程。下一节介绍唯一的块级组合容器：[03. Inline Graph：内联块级子图（SESE 容器）](./03-inline-graphs.md)。
