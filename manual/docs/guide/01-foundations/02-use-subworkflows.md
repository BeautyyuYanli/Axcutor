# 02. Use：跨文件调用子工作流

在 Axcutor 中，任何独立的工作流文件均可作为子节点被其他工作流调用。

当图内节点声明 `use: workflow:<path>`（或调用内建算子 `use: builtin:<op>`）时，运行时为其创建隔离的函数调用栈帧（`Function Frame`）。被调用方拥有独立的 `input`、`output` 与 `branches` 命名空间，无法隐式读取调用方图中的变量，仅通过 `with` 进行按值传参与返回值映射。编译器基于各工作流顶层声明的静态签名（`input`、`output`、`branches`）独立完成类型与定值校验（支持跨文件直接或间接递归调用，运行期受调用栈深度上限保护）。

---

## 一、 `use` 与 `with` 的参数绑定方式

调用节点通过 `with.input` 将当前图的数据绑定至目标工作流的输入参数，并通过 `with.output` 将目标工作流的输出字段映射回当前图的 `variables`。

### 1. 默认同名匹配与显式丢弃未用输出（`null`）

对于 `use: workflow:<path>` 节点（注：内建算子 `use: builtin:<op>` 的必填参数必须显式声明，不支持省略）：

* **省略 `with.input.<param>`**：编译器在当前图作用域中查找与子工作流 `input` 参数同名的顶层变量传入，并在编译期校验其类型等价性与已定值状态。
* **省略 `with.output.<field>`**：子工作流产出的 `output` 字段默认按原字段名写回当前图的 `variables`。
* **显式丢弃未用输出（`with.output.<field>: null`）**：若调用方不需要子工作流的某个输出字段，可在 `with.output` 中将其映射为 `null`。被丢弃的字段不会注入调用方图的符号表，也不会在运行期写回或覆盖调用方的同名局部变量。

```yaml
nodes:
  # 1. 完全省略 with：
  # - 自动将当前图的 raw_text、max_length 传入子工作流同名 input 参数
  # - 自动将子工作流产出的 clean_text、word_count 按原名写回当前图 variables
  normalize_default:
    use: workflow:text/normalize_document

  # 2. 部分省略 with.input 并显式丢弃不需要的 output 字段：
  # - raw_text 显式绑定自 draft_body，其余未列出的 input 参数仍按同名顶层变量匹配
  # - clean_text 映射至 final_body，而不需要的 word_count 设为 null 显式丢弃（不写回当前图）
  normalize_discard_unused:
    use: workflow:text/normalize_document
    with:
      input:
        raw_text: draft_body
      output:
        clean_text: final_body
        word_count: null
```

### 2. 变量名与结构体静态字段路径绑定

当 `with.input.<param>` 的值为裸字符串时，编译器将其解析为当前作用域内的变量引用或结构体字段路径投影：

* **顶层变量名（`<var_name>`）**：绑定当前图已定值的顶层变量。
* **结构体静态字段路径（`<root_var>.<f1>.<f2>`）**：对非空强类型结构体（`type: object` + `properties`）执行编译期静态字段投影，提取嵌套字段值传入（中间穿透步必须列入 `required` 且非空；最末级叶子字段若未列入 `required`，静态投影类型自动提升为 `[<BaseType>, "null"]`，运行期缺失键时求值为 `null`）。

```yaml
nodes:
  normalize_submission:
    use: workflow:text/normalize_document
    with:
      input:
        author_id: current_user_id          # 绑定当前图顶层变量 current_user_id
        raw_text: user_submission.body      # 绑定强类型结构体 user_submission 的嵌套字段 body
      output:
        clean_text: normalized_body         # 将子工作流输出 clean_text 写回当前图变量 normalized_body
        word_count: doc_word_count          # 将子工作流输出 word_count 写回当前图变量 doc_word_count
```

### 3. 常量字面量传递

无需在 `graph.variables` 中声明临时常量变量，可直接在 `with.input` 中内联传递常量字面量：

* **非字符串标量字面量**：整数（`integer`）、浮点数（`number`）、布尔值（`boolean`）或 `null` 直接书写字面量。
* **字符串与复合字面量包装器（`{ value: <literal> }`）**：由于裸字符串表示变量引用，传递字符串字面量、数组字面量或对象字面量时，统一使用 `{ value: <literal> }` 显式包装。

```yaml
nodes:
  normalize_with_literals:
    use: workflow:text/normalize_document
    with:
      input:
        raw_text: user_submission.body
        max_length: 2000                              # 直接传递 integer 字面量
        strip_html: true                              # 直接传递 boolean 字面量
        format_style: { value: "strict" }             # 使用 { value: ... } 传递 string 字面量
        stop_words: { value: ["ad", "spam"] }         # 使用 { value: ... } 传递 array 字面量
```

### 4. 在 `use` 节点上挂载 `for_each` 时的输出对接

当 `use: workflow:<path>` 节点挂载 `for_each` 进行集合遍历时，**禁止声明 `with.output`**。每次迭代产出的子工作流 `output` 字段直接由 `for_each.collect` 或 `for_each.state` 按子工作流原始输出字段名对接并提升至外层图变量（未在 `collect` 或 `state` 中引用的子工作流输出字段自动忽略）：

```yaml
nodes:
  # 对 raw_docs 列表的每个元素调用子工作流 workflow:text/normalize_document
  # 挂载 for_each 时不写 with.output，直接在 for_each.collect 中引用子工作流输出字段 clean_text
  batch_normalize_docs:
    for_each:
      source: raw_docs
      item: current_doc
      concurrency: 4
      collect:
        clean_text: normalized_docs
    use: workflow:text/normalize_document
    with:
      input:
        raw_text: current_doc.body
        max_length: 2000
```

---

## 二、 子工作流控制分支的透明继承

若被调用的子工作流在顶层声明了 `branches`，调用该子工作流的 `use` 节点会在当前图中自动暴露相同的分支集合，并继承各分支关联的 `output` 子集契约。

```yaml
# 被调用文件：workflow:security/audit_content
input:
  text: { type: string }
output:
  certificate_id: { type: string }
  violation_note: { type: string }
branches:
  approved:
    description: "内容合规"
    output: [certificate_id]
  rejected:
    description: "内容违规"
    output: [violation_note]
run: "..."
```

在父工作流中调用该子工作流时，直接在 `edges` 中通过 `<use_node>.<branch> -> target` 路由各分支：

```yaml
# 父工作流片段：直接对 audit_step.approved 与 audit_step.rejected 连边
nodes:
  audit_step:
    use: workflow:security/audit_content
    with:
      input:
        text: normalized_body
      output:
        certificate_id: audit_cert_id
        violation_note: reject_reason

edges:
  - start -> audit_step
  # 仅在 approved 分支下 audit_cert_id 获得定值
  - audit_step.approved -> publish_post
  # 仅在 rejected 分支下 reject_reason 获得定值
  - audit_step.rejected -> notify_author
```

---

## 三、 完整示例：跨文件调用与分支路由

以下完整工作流演示如何组合多个 `use` 节点，串联文档标准化子工作流与合规审计子工作流，并根据子工作流返回的分支走向不同出口：

```yaml
# 1. 父工作流顶层输入与输出契约
input:
  current_user_id: { type: string }
  user_submission:
    type: object
    properties:
      title: { type: string }
      body: { type: string }
    required: [title, body]
output:
  published_url: { type: string }
  reject_reason: { type: string }

# 2. 父工作流对外暴露两个排他分支，分别产出不同输出字段
branches:
  published:
    description: "文章通过审计并成功发布"
    output: [published_url]
  rejected:
    description: "文章未通过合规审计"
    output: [reject_reason]

graph:
  nodes:
    # 步骤 1：调用外部文本标准化子工作流
    # 混合使用结构体字段路径、顶层变量、数值字面量与 { value } 字符串字面量传参，并将未使用的 word_count 设为 null 丢弃
    normalize_doc:
      use: workflow:text/normalize_document
      with:
        input:
          author_id: current_user_id
          raw_text: user_submission.body
          max_length: 5000
          format_style: { value: "strict" }
        output:
          clean_text: normalized_body
          word_count: null

    # 步骤 2：调用带 branches (approved / rejected) 的外部合规审计子工作流
    audit_doc:
      use: workflow:security/audit_content
      with:
        input:
          text: normalized_body
        output:
          certificate_id: audit_cert_id
          violation_note: reject_reason

    # 步骤 3：仅在 audit_doc.approved 分支下执行发布动作
    publish_article:
      input: [user_submission, normalized_body, audit_cert_id]
      output:
        published_url: { type: string }
      run: "使用合规凭证 variables.audit_cert_id 发布标题为 variables.user_submission.title 的正文 variables.normalized_body，将链接写入 output.published_url。"

  # 3. 控制流连边：audit_doc 透明继承子工作流的 approved 与 rejected 分支
  edges:
    - start -> normalize_doc
    - normalize_doc -> audit_doc
    - audit_doc.approved -> publish_article
    - publish_article -> end.published
    - audit_doc.rejected -> end.rejected
```

---

下一节：[03. Inline Graph：内联块级子图（SESE 容器）](./03-inline-graphs.md)。
