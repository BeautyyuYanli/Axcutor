# 01. 文档根结构与三类节点文法规范

本规范定义 Axcutor 工作流文档的源格式约束、基础词法与原语类型、工作流包点分寻址文法、顶层 AST 结构、图内三类节点（`run`、`graph`、`use`）的形态鉴别准则、分支关联输出契约（`BranchSpec`），以及控制流图容器（`GraphSpec`）与节点修饰符子结构（`ForEachSpec`、`RetrySpec`、`CatchSpec`）的形式化良构条件。

---

## 零、 源文档数据模型、基础词法与原语类型定义

### 1. 源文档格式与严格数据模型约束

每个工作流源文档（YAML 或 JSON）在进入 AST 解析前必须符合严格的 JSON 数据模型子集：

* **映射键唯一性与字符串类型约束**：所有映射（Mapping / Object）的键必须为字符串，且同一映射层级内严禁出现重复键（Duplicate Keys）。
* **YAML 结构特性限制**：当源文档格式为 YAML 时，仅允许包含单个根文档（禁止 `---` / `...` 多文档流）；严禁使用锚点（`&anchor`）、别名引用（`*alias`）、合并键（`<<`）及自定义标签；仅支持与 JSON 基础类型一一对应的标准标签（`!!str`、`!!int`、`!!float`、`!!bool`、`!!null`、`!!seq`、`!!map`），拒绝 `!!binary`、`!!timestamp`、`!!set`、`!!omap`、`!!pairs` 等非 JSON 标签。
* **有限数值约束**：所有整数字面量与浮点数字面量必须为有限十进制数，严禁出现 `NaN`、`Inf`、`+Inf` 或 `-Inf`。

### 2. 基础词法非终结符与保留字集合

本规范及后续章节引用以下基础词法非终结符与原语类型：

* **通用标识符（`Identifier`）**：满足正则文法的 ASCII 标识符字符串：
  $$\text{Identifier} ::= \texttt{[a-zA-Z\_][a-zA-Z0-9\_]*}$$
* **全局保留字集合（$\mathcal{R}$）**：由控制流保留字集合 $\mathcal{R}_{\text{cfg}}$ 与宿主代码生成保留关键字集合 $\mathcal{R}_{\text{py}}$（Python 3.12 `keyword.kwlist` 全部 35 个硬关键字）求并构成：
  $$\mathcal{R} = \mathcal{R}_{\text{cfg}} \cup \mathcal{R}_{\text{py}}$$
  其中：
  $$\mathcal{R}_{\text{cfg}} = \{\texttt{"start"}, \texttt{"end"}, \texttt{"fail"}\}$$
  $$\begin{aligned}
  \mathcal{R}_{\text{py}} = \{&\texttt{"False"}, \texttt{"None"}, \texttt{"True"}, \texttt{"and"}, \texttt{"as"}, \texttt{"assert"}, \texttt{"async"}, \texttt{"await"}, \\
  &\texttt{"break"}, \texttt{"class"}, \texttt{"continue"}, \texttt{"def"}, \texttt{"del"}, \texttt{"elif"}, \texttt{"else"}, \texttt{"except"}, \\
  &\texttt{"finally"}, \texttt{"for"}, \texttt{"from"}, \texttt{"global"}, \texttt{"if"}, \texttt{"import"}, \texttt{"in"}, \texttt{"is"}, \\
  &\texttt{"lambda"}, \texttt{"nonlocal"}, \texttt{"not"}, \texttt{"or"}, \texttt{"pass"}, \texttt{"raise"}, \texttt{"return"}, \texttt{"try"}, \\
  &\texttt{"while"}, \texttt{"with"}, \texttt{"yield"}\}
  \end{aligned}$$
* **受限标识符子类型（`RestrictedIdentifier`）**：排除了全局保留字集合 $\mathcal{R}$ 的合法标识符：
  $$\text{RestrictedIdentifier} = \text{NodeId} = \text{BranchId} = \text{CatchBranchId} ::= \text{Identifier} \setminus \mathcal{R}$$
  工作流中的所有用户自定义符号名——包括节点名 `NodeId`、正常分支名 `BranchId`、异常捕获分支名 `CatchBranchId`、顶层或节点的 `input` / `output` 变量名、`graph.variables` 变量名、`for_each` 的 `item` / `key` / `index` 绑定名，以及 `TypedDict`（`type: object` + `properties`）中的属性字段名——均必须属于 $\text{Identifier} \setminus \mathcal{R}$。
* **包路径段与点分包寻址文法（`PackageSegment`、`PackagePath`、`LocalModuleSpec`、`UseTargetSpec`）**：
  * **包路径段（`PackageSegment`）**：排除了 Python 硬关键字集合 $\mathcal{R}_{\text{py}}$ 的合法 ASCII 标识符，按是否以下划线 `_` 开头划分为公开路径段（`PublicSegment`）与内部私有路径段（`PrivateSegment`）：
    $$\text{PackageSegment} ::= \text{Identifier} \setminus \mathcal{R}_{\text{py}}$$
    $$\text{PublicSegment} ::= \{ s \in \text{PackageSegment} \mid \lnot \text{starts\_with}(s, \texttt{"\_"}) \}$$
    $$\text{PrivateSegment} ::= \{ s \in \text{PackageSegment} \mid \text{starts\_with}(s, \texttt{"\_"}) \}$$
  * **点分工作流包路径（`PackagePath`）**：每个具名工作流对应逻辑命名空间中的一个点分包路径（物理目录树中唯一对应一个包含 `__axcutor__.yaml` 的文件夹），首段（顶层挂载根包名 `RootPackageName`）必须为 `PublicSegment` 且不得为系统保留入口名 `"__main__"`：
    $$\text{PackagePath} ::= \text{PublicSegment} \; ( \texttt{"."} \; \text{PackageSegment} )^* \quad (\text{首段 } \neq \texttt{"\_\_main\_\_"})$$
    *(注：独立匿名入口文档使用系统保留标识 `"__main__"`，不属于可挂载或可被调用的 `PackagePath`；严禁在 `PackagePath` 中出现斜杠 `/`、相对路径段 `.` / `..`、`__axcutor__` 或 `.yaml` / `.yml` / `.json` 文件扩展名。)*
  * **包内私有 Python 模块单点相对路径（`LocalModuleSpec`）**：用于 `executor: "module"` 绑定当前工作流包文件夹内部的 `.py` 实现模块，仅允许单点包内相对路径（禁止跨包绝对路径与 `..` 上级相对路径）：
    $$\text{LocalModuleSpec} ::= \texttt{"."} \mid \texttt{"."} \; \text{LocalPath}, \quad \text{LocalPath} ::= \text{PackageSegment} \; ( \texttt{"."} \; \text{PackageSegment} )^*$$
  * **调用目标规格（`UseTargetSpec`）**：`CallNode` 中 `use` 字段的值类型，仅支持跨包工作流调用（从根包起始的绝对点分包路径 `PackagePath`）或内建确定性算子调用：
    $$\text{UseTargetSpec} ::= \texttt{"workflow:"} \; \text{PackagePath} \mid \texttt{"builtin:"} \; \text{BuiltinOp}$$
* **数值原语类型（`PositiveInt`、`NonNegativeInt`）**：
  $$\text{PositiveInt} = \mathbb{Z}_{\ge 1} = \{n \in \mathbb{Z} \mid n \ge 1\}, \quad \text{NonNegativeInt} = \mathbb{Z}_{\ge 0} = \{n \in \mathbb{Z} \mid n \ge 0\}$$
* **原子执行体规格（`RunSpec`）**：`run` 字段的值类型，必须是以 `executor` 字段作为唯一鉴别键（Discriminator）的带标签联合映射对象（Tagged Union Mapping），不支持裸字符串简写：
  $$\text{RunSpec} ::= \text{BuiltinRunSpec} \mid \text{ModuleRunSpec} \mid \text{ExtensibleExecutorRunSpec}$$
  $$\text{BuiltinRunSpec} ::= \{\; \texttt{executor}: \texttt{"builtin"},\; \texttt{content}: \text{NonEmptyString} \;\}$$
  $$\text{ModuleRunSpec} ::= \{\; \texttt{executor}: \texttt{"module"},\; \texttt{content}: \text{LocalModuleSpec} \;\}$$
  $$\text{ExtensibleExecutorRunSpec} ::= \{\; \texttt{executor}: \text{Identifier} \setminus \{\texttt{"builtin"}, \texttt{"module"}\},\; \dots \text{（执行器特定配置字段）} \;\}$$
  其中：
  * `executor`（必填）：非空合法标识符（`Identifier`），标识目标执行器类型；
  * 当 `executor == "builtin"` 时（`BuiltinRunSpec`）：必须且只能包含 `executor: "builtin"` 与 `content: NonEmptyString`（满足 $|\text{trim}(\text{content})| \ge 1$ 的 Python 源码文本）两个键，出现任何额外未知字段均在编译期报 `schema.unknown_field` 错误。其代码生成与运行期契约详见 [`02-builtin-python-executor.md`](../05-executor-abi/02-builtin-python-executor.md)。
  * 当 `executor == "module"` 时（`ModuleRunSpec`）：必须且只能包含 `executor: "module"` 与 `content: LocalModuleSpec` 两个键（出现任何额外未知字段均报 `schema.unknown_field` 错误；若 `content` 不满足单点相对路径文法 `LocalModuleSpec`，在 `/run/content` 处报 `schema.run` 错误；若所属工作流为匿名入口 `"__main__"`，编译期报 `module.anonymous_package` 错误）。其包同构布局、契约模板 SHA 校验与运行期契约详见 [`03-module-python-executor.md`](../05-executor-abi/03-module-python-executor.md)。

---

## 一、 顶层根节点（`RootNode`）文法

每个工作流文件反序列化后必须是一个映射对象（`RootNode`），满足以下文法约束：

1. **必填契约字段**：
   * `input: Map<RestrictedIdentifier, TypeSpec>`（可为空映射 `{}`）
   * `output: Map<RestrictedIdentifier, TypeSpec>`（可为空映射 `{}`）
2. **可选契约字段**：
   * `branches: Map<BranchId, BranchSpec>`（默认 `{}`）
3. **执行体互斥鉴别（Mutual Exclusion）**：
   * `RootNode` **必须且只能在 `run: RunSpec` 与 `graph: GraphSpec` 两个键中声明其一**：
     * **当包含 `run`（`RootRunNode`）**：允许额外声明 `timeout_ms?: PositiveInt` 与 `retry?: RetrySpec`；禁止声明 `for_each`、`catch`、`with`、`variables`、`nodes`、`edges`、`max_steps`。
     * **当包含 `graph`（`RootGraphNode`）**：禁止在顶层声明 `timeout_ms`、`retry`、`for_each`、`catch`、`with`。

---

## 二、 图内节点（`InnerNode`）的三态鉴别与字段约束

在 `graph.nodes: Map<NodeId, InnerNode>` 中，每个键 `<NodeId>` 必须为合法 `NodeId`（即属于 $\text{Identifier} \setminus \mathcal{R}$，**禁止使用保留字 $\mathcal{R}$ 中的任何标识符**）。

每个 `InnerNode` 必须通过鉴别键 **`run: RunSpec`、`graph: GraphSpec`、`use: UseTargetSpec` 三选一** 唯一确定其 AST 子类型：

| AST 节点类型 | 唯一鉴别键 | 允许的可选字段 | 禁止出现的字段 |
| :--- | :--- | :--- | :--- |
| **`ActionNode`** | `run` | `input: List<RestrictedIdentifier>`<br>`output: Map<RestrictedIdentifier, TypeSpec>`<br>`branches: Map<BranchId, BranchSpec>`<br>`for_each: ForEachSpec`<br>`timeout_ms: PositiveInt`<br>`retry: RetrySpec`<br>`catch: CatchSpec` | `with`, `graph`, `use` |
| **`InlineGraphNode`** | `graph` | `input: List<RestrictedIdentifier>`<br>`output: Map<RestrictedIdentifier, TypeSpec>`<br>`branches: Map<BranchId, BranchSpec>`<br>`for_each: ForEachSpec`<br>`timeout_ms: PositiveInt`<br>`retry: RetrySpec`<br>`catch: CatchSpec` | `with`, `run`, `use` |
| **`CallNode`** | `use` | `with: WithSpec`<br>*(以下仅当 `use` 为 `workflow:<pkg_path>` 时允许，为 `builtin:<op>` 时禁止)*：<br>`for_each: ForEachSpec`<br>`timeout_ms: PositiveInt`<br>`retry: RetrySpec`<br>`catch: CatchSpec` | `input`, `output`, `branches`, `run`, `graph` |

* **`input` 列表去重约束**：在 `ActionNode` 与 `InlineGraphNode` 中，`input` 若声明，必须为无重复元素的受限顶层变量标识符列表 `List<RestrictedIdentifier>`。

---

## 三、 分支关联输出契约（`BranchSpec`）规范化

在 `RootNode`、`ActionNode` 或 `InlineGraphNode` 的 `branches: Map<BranchId, BranchSpec>` 中：

* 每个 `<BranchId>` 必须为合法 `BranchId`（即属于 $\text{Identifier} \setminus \mathcal{R}$），且不可与同节点 `catch` 中的异常分支名重复。
* 每个 `BranchSpec` 在 AST 规范化阶段统一消糖为二元组 `(description: String, output_subset: Set<RestrictedIdentifier>)`：
  1. **简写形式**（`<choice>: "<description>"`）：消糖为 `output_subset = dom(node.output)`（即承诺产出该节点 `output` 声明的全部字段）。
  2. **显式子集形式**（`<choice>: { description: "<description>", output: [<field_1>, ...] }`）：
     * `output` 必须为无重复标识符列表；
     * **子集良构性条件**：$\text{set}(\text{branch.output}) \subseteq \text{dom}(\text{node.output})$。若引用了未在当前节点 `output` 中声明的字段名，编译期报错。

---

## 四、 控制流图容器（`GraphSpec`）文法与良构性公理

在 `RootGraphNode.graph` 或 `InlineGraphNode.graph` 中，`GraphSpec` 必须是一个映射对象，且仅允许包含以下四个字段：

1. **`nodes: Map<NodeId, InnerNode>`（必填）**：
   * 必须为非空映射：$\text{dom}(\text{nodes}) \neq \emptyset$。
2. **`edges: List<EdgeExpr>`（必填）**：
   * 必须为非空字符串数组：$|\text{edges}| \ge 1$（每条边的 EBNF 文法见 [`03-edge-and-cfg-grammar.md`](./03-edge-and-cfg-grammar.md)）。
3. **`max_steps?: PositiveInt`（可选，默认 `1000`）**：
   * 若显式声明，必须为正整数（$\text{max\_steps} \ge 1$）。
4. **`variables?: Map<RestrictedIdentifier, VarDecl>`（可选，默认 `{}`）**：
   * 每个键 `<v>` 必须为合法受限标识符（$\text{Identifier} \setminus \mathcal{R}$）。
   * 每个值 `<val>`（`VarDecl`）必须是一个映射对象，其除去可选键 `default` 后的其余键值映射必须构成一个良构的 [`TypeSpec`](../02-type-system-and-symbols/01-typespec-well-formedness.md)。
   * **省略 `default` 与 `default: null` 的语义区分**：若 `<val>` 显式包含键 `default`（记为 $v \in \text{InitVars}(G)$），则表示该变量在图启动时持有初始默认值 `default_literal`（含显式 `default: null`，此时要求 $\text{Nullable}(\text{TypeSpec}_v) = \texttt{true}$）；若 `<val>` 未包含键 `default`，则仅声明该图级局部变量的静态类型，图启动时处于未定值状态（`Unbound`）。

---

## 五、 节点修饰符子结构（`ForEachSpec`、`RetrySpec`、`CatchSpec`）良构性公理

### 1. 集合遍历修饰符（`ForEachSpec`）

当 `ActionNode`、`InlineGraphNode` 或 `CallNode`（仅限 `use: workflow:<pkg_path>`）声明 `for_each` 时，其校验职责严格划分为 **阶段 01（AST 结构良构性）** 与 **阶段 02（静态类型与符号表检查）** 两层：

#### (a) 阶段 01：AST 语法与结构良构条件（Phase 1 Structural Well-Formedness）

`for_each` 的值必须为仅包含以下 5 个输入切片与并发控制属性的映射对象，且满足节点级结构约束：

* **`source: FieldPathExpr`（必填）**：必须为符合字段路径文法的非空表达式（文法见 [`02-binding-and-projection-grammar.md`](./02-binding-and-projection-grammar.md)）。
* **`item: RestrictedIdentifier`（必填）**：注入单次迭代局部帧的元素（当源为 `array` 时）或值（当源为 `OrderedMap` 时）变量名（属于 $\text{Identifier} \setminus \mathcal{R}$）。
* **`key?: RestrictedIdentifier`（可选）**：注入单次迭代映射键（`string`）的变量名（属于 $\text{Identifier} \setminus \mathcal{R}$）。
* **`index?: RestrictedIdentifier`（可选）**：注入单次迭代序号（`0, 1, ...`，类型 `integer`）的变量名（属于 $\text{Identifier} \setminus \mathcal{R}$）。
* **`concurrency?: PositiveInt`（可选，默认 `1`）**：必须为正整数（$\text{concurrency} \ge 1$）。
* **挂载 `for_each` 节点的有效输出非空约束**：
  * 挂载了 `for_each` 的节点 $N$ 必须向外层图声明至少一个目标输出变量（即 $OutVars(N) \neq \emptyset$）：
    * 当 $N$ 为 `ActionNode` 或 `InlineGraphNode` 时：$\text{dom}(N.\text{output}) \neq \emptyset$；
    * 当 $N$ 为 `CallNode`（`use: workflow:<pkg_path>`）时：经 $N.\text{with.output}$ 绑定的有效目标变量集合非空（$\text{values}(N.\text{with.output}) \neq \emptyset$）。

#### (b) 阶段 02：源集合静态类型与局部符号约束（Phase 2 Static Type Constraints）

在阶段 02 符号表构建与两阶段符号解析期间（完整规则详见 [`02-type-system-and-symbols/03-two-phase-symbol-resolution.md`](../02-type-system-and-symbols/03-two-phase-symbol-resolution.md)），针对外层图符号表 $\Sigma_{\text{outer}}$ 执行以下静态类型校验：

* **`source` 非空集合类型约束**：
  * 令 $T_{\text{src}} = \text{TypeOf}(\Sigma_{\text{outer}}, \text{source})$。必须满足：
    $$\text{Nullable}(T_{\text{src}}) = \texttt{false} \;\land\; \text{BaseType}(T_{\text{src}}) \in \{\texttt{array}, \texttt{object (OrderedMap)}\}$$
    若 $T_{\text{src}}$ 可空（含 `null`）或为固定属性结构体（`TypedDict`）或标量类型，编译期报错。
* **`key` 字段的源类型相容约束**：
  * **仅当 $T_{\text{src}}$ 为 `OrderedMap`（`type: object` + `additionalProperties`）时允许声明 `key`**；
  * 若 $\text{BaseType}(T_{\text{src}}) = \texttt{array}$ 且显式声明了 `key`，编译期报错。

### 2. 原地快照重试修饰符（`RetrySpec`）

当 `RootRunNode`、`ActionNode`、`InlineGraphNode` 或 `CallNode`（仅限 `use: workflow:<pkg_path>`）声明 `retry` 时，其值必须为满足以下良构性公理的映射对象：

* **`max_attempts: PositiveInt`（必填）**：必须为 $\ge 2$ 的正整数（含首次执行在内至少允许 2 次尝试）。
* **`backoff_ms?: NonNegativeInt`（可选，默认 `0`）**：必须为 $\ge 0$ 的非负整数。\n* **`on?: List<RecoverableSelector>`（可选，默认 `["execution", "timeout"]`）**：
  * 必须为非空且无重复元素的列表，其中每个元素必须属于可恢复异常选择器全集：
    $$\mathcal{S}_{\text{all}} = \{\texttt{"execution"}, \texttt{"timeout"}, \texttt{"contract"}, \texttt{"max\_steps"}\}$$
  * **原子 `run` 节点禁止包含 `"max_steps"`**：当所属节点为 `ActionNode` 或 `RootRunNode` 时，因原子 `run` 执行体不存在图步数计数器，必须满足：
    $$\texttt{"max\_steps"} \notin \text{set}(\text{retry.on})$$
    否则编译期报错。

### 3. 异常捕获修饰符（`CatchSpec`）

当 `ActionNode`、`InlineGraphNode` 或 `CallNode`（仅限 `use: workflow:<pkg_path>`）声明 `catch` 时，其值必须为非空映射 `Map<CatchBranchId, List<RecoverableSelector> | "*">`，且满足以下良构性公理：

* **分支名合法性与互斥约束**：
  * 每个键 $c \in \text{dom}(N.\text{catch})$ 必须为合法 `CatchBranchId`（即属于 $\text{Identifier} \setminus \mathcal{R}$）；
  * 禁止与同一节点的正常业务分支重名：$\text{dom}(N.\text{catch}) \cap \text{dom}(N.\text{branches}) = \emptyset$。
* **选择器完备与互斥约束**：
  * 定义节点 $N$ 允许捕获的选择器论域 $\mathcal{S}(N)$：
    * 若 $N$ 为 `ActionNode`（`run`）：$\mathcal{S}(N) = \{\texttt{"execution"}, \texttt{"timeout"}, \texttt{"contract"}\}$（**显式列表中禁止包含 `"max_steps"`**）；
    * 若 $N$ 为 `InlineGraphNode`（`graph`）或 `CallNode`（`use: workflow:<pkg_path>`）：$\mathcal{S}(N) = \{\texttt{"execution"}, \texttt{"timeout"}, \texttt{"contract"}, \texttt{"max\_steps"}\}$。
  * 对每个显式列表分支 $c$，其值必须为 $\mathcal{S}(N)$ 的非空无重复子集，且不同分支的显式选择器集合之间必须两两无交集；
  * 同一 `catch` 块中最多允许 **1 个**分支将值设为通配符 `"*"`，其在 AST 规范化阶段展开为剩余可恢复异常集合 $\mathcal{S}(N) \setminus \bigcup_{c' \neq c} \text{set}(N.\text{catch}[c'])$（展开后若为空集则编译期报错）。
