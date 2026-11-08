# 02. Use：工作流包与跨包调用

在 Axcutor 中，可复用的工作流以**工作流包（Workflow Package）**为单位组织，任何工作流包均可作为子节点被其他工作流通过点分包路径调用。

当图内节点声明 `use: workflow:<pkg_path>`（或调用内建算子 `use: builtin:<op>`）时，运行时为其创建隔离的函数调用栈帧（`Function Frame`）。被调用方拥有独立的 `input`、`output` 与 `branches` 命名空间，无法隐式读取调用方图中的变量，仅通过 `with` 进行按值传参与返回值映射。编译器基于各工作流包顶层声明的静态签名（`input`、`output`、`branches`）独立完成类型、可见性与定值校验（支持跨包直接或间接递归调用，运行期受调用栈深度上限保护）。

---

## 一、 工作流包模型与 `_` 前缀可见性规则

### 1. 一目录一工作流包（One Directory = One Workflow Package）

Axcutor 将工作流包与标准 Python 包统一为同构的目录模型：

* **包清单文件（`__axcutor__.yaml`）**：任何包含 `__axcutor__.yaml`（以及供 Python 识别的 `__init__.py`）的目录即构成一个工作流包，其点分包路径（`PackagePath`，如 `billing.charge` 或 `text.normalize_document`）即为全局唯一的 `WorkflowId`。
* **统一绝对点分包路径寻址**：`use: workflow:<pkg_path>` 严格使用点号 `.` 分隔的逻辑包路径（不含 `.yaml`/`.json` 扩展名、不含路径斜杠 `/`、不支持 `.` 或 `..` 相对工作流寻址）。
* **单文件入口工作流（`"__main__"`）**：临时脚本或快速原型也可直接以独立单文件（如 `main.yaml`）作为编译与运行入口。此时编译器赋予其保留匿名身份 `"__main__"`——它可以调用任意公开工作流包，但自身不能被挂载进包命名空间，不能被 `use: workflow:__main__` 调用，也不能声明 `executor: module`。

### 2. 编译期 `_` 前缀私有包可见性（Private Package Visibility）

工作流包路径的**首段（根包名）**必须是以字母开头的公开标识符（如 `billing`、`text`）；从**第二段起**，包段名称允许以单下划线 `_` 开头（如 `_internal`、`_risk_score`），声明该子包及其子树为**父包私有**：

* **归属边界**：若目标包路径 $T = s_1 . s_2 \dots s_k$ 中存在以 `_` 开头的私有段 $s_m$（$m \ge 2$），则 $T$ 归属于其紧邻父包前缀 $P_{\text{owner}} = s_1 . s_2 \dots s_{m-1}$。调用方工作流 $C$ 必须位于每一个私有段对应 $P_{\text{owner}}$ 的同一子树内（即 $C = P_{\text{owner}}$ 或 $C$ 以 `$P_{\text{owner}}.$` 为前缀），否则编译器直接拒绝并报告 `visibility.private_workflow`。
* **示例**：
  * `billing._risk_score` 仅允许被 `billing`、`billing.charge` 或 `billing._risk_score.helper` 等 `billing` 子树内的工作流调用；若 `orders.checkout` 或匿名入口 `"__main__"` 尝试调用 `use: workflow:billing._risk_score`，将在编译期被拦截。
  * 不含 `_` 前缀段的公开包（如 `billing.charge`、`text.normalize_document`）对所有工作流及 `"__main__"` 公开可见。

```text
workflows/
└── billing/
    ├── __init__.py
    ├── __axcutor__.yaml              # 公开根包：workflow:billing
    ├── charge/
    │   ├── __init__.py
    │   ├── __axcutor__.yaml          # 公开子包：workflow:billing.charge
    │   ├── capture.py                # 包内模块：run: { executor: module, content: ".capture" }
    │   └── capture_contract.py       # 自动生成的强类型契约桩
    └── _risk_score/
        ├── __init__.py
        └── __axcutor__.yaml          # 私有子包：workflow:billing._risk_score（仅 billing 子树可见）
```

---

## 二、 `use` 与 `with` 的参数绑定方式

调用节点通过 `with.input` 将当前图的数据显式绑定至目标工作流包的输入参数，并通过 `with.output` 将需要的目标工作流包输出字段显式映射回当前图的 `variables`（接收返回值的目标变量需已在当前图符号表中声明类型）。

### 1. 显式入参绑定、按需输出接收与子类型兼容

对于 `use: workflow:<pkg_path>` 节点，参数绑定遵循显式边界与单向子类型兼容原则：

* **`with.input` 必填参数显式绑定与入参子类型兼容**：被调用子工作流顶层声明的每一个 `input` 参数，均必须在 `with.input` 中显式写出（若调用方变量与参数同名，显式写作 `<param>: <param>`；仅当被调用工作流 `input: {}` 为空时可省略 `with.input`）。传入实参的类型只需是目标输入参数声明类型的**子类型**——例如可将非空 `string` 传给子工作流的可选参数 `[string, "null"]`、将 `integer` 传给 `number` 参数、将带 `enum` 的分类字符串传给普通 `string` 参数，或将 `Array<integer>` 协变传给 `Array<number>`（详见 [TypeSpec 速查表：静态子类型与容器协变](../../reference/03-typespec-cheatsheet.md)）。
* **`with.output` 按需映射与出参子类型兼容**：在 `with.output` 中仅需列出调用方需要接收的输出字段 `<target_field>: <local_var>`（其中 `<local_var>` 为合法顶层标识符，禁止设为 `null`）。子工作流输出字段的类型只需是当前图中目标接收变量声明类型的**子类型**，即允许父图使用更宽泛的变量类型（如可空类型、父枚举或 `number`）接收子工作流产出的精确类型。未在 `with.output` 中列出的其余子工作流输出字段默认自动忽略，既不注入调用方符号表，也不会在被调用工作流升级新增输出字段时污染或冲突调用方命名空间。

```yaml
graph:
  # 仅由 use 节点写入的局部中间变量在 variables 中显式声明 TypeSpec
  variables:
    clean_text: { type: string }
    word_count: { type: number }          # 允许用更宽泛的 number 接收子工作流产出的 integer
    final_body: { type: [string, "null"] } # 允许用可空 [string, "null"] 接收子工作流产出的非空 string

  nodes:
    # 1. 显式传入全部目标 input 参数，并接收 clean_text 与 word_count 两个输出字段：
    normalize_full:
      use: workflow:text.normalize_document
      with:
        input:
          raw_text: raw_text
          max_length: max_length
        output:
          clean_text: clean_text
          word_count: word_count

    # 2. 按需接收部分 output 字段：
    # - 显式传入 raw_text 与 max_length 变量
    # - 仅绑定需要的 clean_text -> final_body；未列出的 word_count 自动忽略，无需额外编写丢弃语法
    normalize_only_text:
      use: workflow:text.normalize_document
      with:
        input:
          raw_text: draft_body
          max_length: max_length
        output:
          clean_text: final_body
```

### 2. 变量名与结构体静态字段路径绑定

当 `with.input.<param>` 的值为裸字符串时，编译器将其解析为当前作用域内的变量引用或结构体字段路径投影：

* **顶层变量名（`<var_name>`）**：绑定当前图已定值的顶层变量。
* **结构体静态字段路径（`<root_var>.<f1>.<f2>`）**：对非空强类型结构体（`type: object` + `properties`）执行编译期静态字段投影，提取嵌套字段值传入（中间穿透步必须列入 `required` 且非空；最末级叶子字段若未列入 `required`，静态投影类型自动提升为 `[<BaseType>, "null"]`，运行期缺失键时求值为 `null`）。

```yaml
graph:
  variables:
    normalized_body: { type: string }
    doc_word_count: { type: integer }

  nodes:
    normalize_submission:
      use: workflow:text.normalize_document
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
    use: workflow:text.normalize_document
    with:
      input:
        raw_text: user_submission.body
        max_length: 2000                              # 直接传递 integer 字面量
        strip_html: true                              # 直接传递 boolean 字面量
        format_style: { value: "strict" }             # 使用 { value: ... } 传递 string 字面量
        stop_words: { value: ["ad", "spam"] }         # 使用 { value: ... } 传递 array 字面量
      output:
        clean_text: normalized_body
```

> **提示**：`use: workflow:<pkg_path>` 节点同样支持挂载 `for_each` 进行批量调用，详见 [01. For-Each：静态集合并发映射](../03-concurrency/01-for-each.md)。

---

## 三、 子工作流控制分支的透明继承

若被调用的子工作流在顶层声明了 `branches`，调用该子工作流的 `use` 节点会在当前图中自动暴露相同的分支集合，并继承各分支关联的 `output` 子集契约。

```yaml
# 被调用工作流包：security/audit_content/__axcutor__.yaml（workflow:security.audit_content）
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
run:
  executor: builtin
  content: |
    if "forbidden" in input.text:
        return Branch.rejected(violation_note="Contains forbidden keyword")
    return Branch.approved(certificate_id="CERT-001")
```

在父工作流中调用该子工作流时，直接在 `edges` 中通过 `<use_node>.<branch> -> target` 路由各分支：

```yaml
# 父工作流片段：直接对 audit_step.approved 与 audit_step.rejected 连边
graph:
  variables:
    audit_cert_id: { type: string }
    reject_reason: { type: string }

  nodes:
    audit_step:
      use: workflow:security.audit_content
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

## 四、 完整示例：跨包调用与分支路由

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
  # 3. 显式声明仅由 use 子工作流写入的图内局部中间变量类型（reject_reason 已在顶层 output 中声明）
  variables:
    normalized_body: { type: string }
    audit_cert_id: { type: string }

  nodes:
    # 步骤 1：调用外部文本标准化工作流包
    # 混合使用结构体字段路径、顶层变量、数值字面量与 { value } 字符串字面量传参；仅绑定 clean_text（未列出的 word_count 自动忽略）
    normalize_doc:
      use: workflow:text.normalize_document
      with:
        input:
          author_id: current_user_id
          raw_text: user_submission.body
          max_length: 5000
          format_style: { value: "strict" }
        output:
          clean_text: normalized_body

    # 步骤 2：调用带 branches (approved / rejected) 的外部合规审计工作流包
    audit_doc:
      use: workflow:security.audit_content
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
      run:
        executor: builtin
        content: |
          slug = input.user_submission.title.lower().replace(" ", "-")
          url = f"https://example.com/posts/{slug}?cert={input.audit_cert_id}"
          return Output(published_url=url)

  # 4. 控制流连边：audit_doc 透明继承子工作流的 approved 与 rejected 分支
  edges:
    - start -> normalize_doc
    - normalize_doc -> audit_doc
    - audit_doc.approved -> publish_article
    - publish_article -> end.published
    - audit_doc.rejected -> end.rejected
```

---

下一节：[03. Inline Graph：内联块级子图（SESE 容器）](./03-inline-graphs.md)。
