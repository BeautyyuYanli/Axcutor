# 01. `TypeSpec` 良构性公理（Well-Formedness Rules）

Axcutor 的类型系统采用标准 JSON Schema 的**闭合强类型子集（`TypeSpec`）**。静态检查器在编译期首先对所有出现的 `TypeSpec` 执行良构性校验（Well-Formedness Validation），不满足以下公理的 Schema 在编译期被拒绝。通过校验后，编译器前端将其归一化为标准化的 JSON Schema `TypeSpec` 对象（可空类型统一规范化为 `type: [b, "null"]`，`TypedDict` 显式填充 `required` 列表，且保留声明字段顺序），直接作为跨组件与执行器边界的权威类型表示，无需引入额外的中间表示（IR）。

---

## 一、 基础类型域与可空性修饰公理

设基础非空类型集合为：
$$\mathcal{B} = \{\texttt{"string"}, \texttt{"integer"}, \texttt{"number"}, \texttt{"boolean"}, \texttt{"array"}, \texttt{"object"}, \texttt{"handle"}\}$$

1. **禁止 `any`、裸 `"null"` 与多类型联合**：
   * 每个 `TypeSpec` 必须包含 `type` 字段。
   * `type` 的值只能是以下两种形式之一：
     * **非空类型**：$b \in \mathcal{B}$；
     * **显式可空类型（`Optional<T>`）**：长度为 2 的数组 $[b, \texttt{"null"}]$ 或 $[\texttt{"null"}, b]$，其中 $b \in \mathcal{B}$。
   * 禁止省略 `type`、禁止 `type: "null"` 单独出现、禁止两个不同基础类型联合（如 `["string", "integer"]`）。

2. **基础类型与可空性辅助投影函数**：
   对任意良构的 `TypeSpec` $T$，定义以下四个编译期基础投影与变换函数：
   * **基础非空类型提取 $\text{BaseType}(T) \in \mathcal{B}$**：
     $$\text{BaseType}(T) = \begin{cases} b, & \text{若 } T.\texttt{type} = b \\ b, & \text{若 } T.\texttt{type} \in \{[b, \texttt{"null"}], [\texttt{"null"}, b]\} \end{cases}$$
   * **可空性判定谓词 $\text{Nullable}(T) \in \{\text{true}, \text{false}\}$**：
     $$\text{Nullable}(T) = \begin{cases} \text{true}, & \text{若 } T.\texttt{type} \in \{[b, \texttt{"null"}], [\texttt{"null"}, b]\} \\ \text{false}, & \text{若 } T.\texttt{type} = b \end{cases}$$
   * **去空变换 $\text{StripNull}(T)$**：将 $T$ 的顶层 `type` 字段规范化为单一基础类型 $\text{BaseType}(T)$，并完整保留 $T$ 的其余结构字段（`enum`、`items`、`properties`、`required`、`additionalProperties`、`kind`、`description`），满足 $\text{Nullable}(\text{StripNull}(T)) = \text{false}$。
   * **加空变换 $\text{MakeNullable}(T)$**：将 $T$ 的顶层 `type` 字段规范化为 $[\text{BaseType}(T), \texttt{"null"}]$（幂等操作：若 $T$ 已可空则保持可空），并完整保留 $T$ 的其余结构字段（`enum`、`items`、`properties`、`required`、`additionalProperties`、`kind`、`description`），满足 $\text{Nullable}(\text{MakeNullable}(T)) = \text{true}$。

---

## 二、 五类构造器的递归闭合性公理

设 $b = \text{BaseType}(T) \in \mathcal{B}$。任意良构的 `TypeSpec` $T$ 必须且只能归属于以下五类构造器之一，并满足对应的递归闭合性约束。

> **对象构造器互斥铁律（`TypedDict` vs `OrderedMap`）**：当 $b = \texttt{"object"}$ 时，**构造器 3（`TypedDict`）与构造器 4（`OrderedMap`）严格互斥且完备**——凡基础类型为 `object` 的 `TypeSpec`，必须且只能在声明 `properties`（形态 3）与声明 `additionalProperties`（形态 4）之间二选一，严禁两者同时出现，也严禁两者同时省略。

### 1. 基础标量构造器（$b \in \{\texttt{"string"}, \texttt{"integer"}, \texttt{"number"}, \texttt{"boolean"}\}$）
* 允许可选字段 `enum: List<Literal>` 与 `description: String`；禁止出现 `items`、`properties`、`required`、`additionalProperties`、`kind`。
* 若声明了 `enum`：
  * `enum` 必须为非空且无重复元素的字面量数组；
  * `enum` 中的每一个元素必须属于基础类型 $b$（不允许包含 `null`，可空性统一由 `type: [b, "null"]` 表达）。

### 2. 强类型列表构造器（$b = \texttt{"array"}$）
* **必填字段**：必须声明 `items: TypeSpec`，且 `items` 的值必须是一个递归良构的 `TypeSpec`（禁止 `items` 为数组或布尔值）。
* **互斥与闭合约束**：允许可选字段 `description: String`；禁止出现 `properties`、`required`、`additionalProperties`、`enum`、`kind`。

### 3. 强类型结构体构造器（`TypedDict`：$b = \texttt{"object"} \land \texttt{properties} \in \text{dom}(T)$）
* **必填字段**：`properties: Map<RestrictedIdentifier, TypeSpec>` 必须为**非空映射**，每个键必须为排除了全局保留字集合 $\mathcal{R}$ 的受限标识符（$\text{Identifier} \setminus \mathcal{R}$，详见 [`01-document-and-node-grammar.md`](../01-grammar-and-ast/01-document-and-node-grammar.md)），每个值必须为递归良构的 `TypeSpec`。
* **可选字段 `required: List<RestrictedIdentifier>`**：
  * 若省略 `required`，编译器将其规范化为 `required = dom(properties)`（默认全部字段必填）；
  * 若显式声明 `required`，其元素必须无重复且满足 $\text{set}(\text{required}) \subseteq \text{dom}(\text{properties})$。
* **互斥与闭合约束**：允许可选字段 `description: String`；禁止出现 `additionalProperties`、`items`、`enum`、`kind`。运行时校验时视为 `additionalProperties: false`（闭合结构体，不允许未声明的额外字段）。

### 4. 强类型有序映射构造器（`OrderedMap`：$b = \texttt{"object"} \land \texttt{additionalProperties} \in \text{dom}(T)$）
* **必填字段**：`additionalProperties: TypeSpec` 必须是一个递归良构的 `TypeSpec`（禁止将 `additionalProperties` 设为布尔值 `true` 或 `false`）。
* **互斥与闭合约束**：允许可选字段 `description: String`；禁止出现 `properties`、`required`、`items`、`enum`、`kind`。

### 5. 不透明资源句柄构造器（`Handle`：$b = \texttt{"handle"}$）
* **必填字段**：必须声明 `kind: HandleKind`，其值必须满足名义资源标识文法：
  $$\text{HandleKind} ::= \text{ModulePath} \; \texttt{":"} \; \text{PublicClassName}$$
  $$\text{ModulePath} ::= \text{PackagePath}, \quad \text{PublicClassName} ::= \text{PublicSegment}$$
  其中：
  * `ModulePath` 复用点分包路径文法 `PackagePath`（首段必须为公开路径段 $\text{PublicSegment} \neq \texttt{"\_\_main\_\_"}$，各段属于 $\text{Identifier} \setminus \mathcal{R}_{\text{py}}$，且严禁出现保留段名 `__axcutor__` 或 `__ax_resources__`）；
  * `PublicClassName` 必须为不以下划线 `_` 开头的公开标识符段 $\text{PublicSegment}$（严禁以下划线 `_` 开头或使用 Python 硬关键字 $\mathcal{R}_{\text{py}}$）。
* **互斥与闭合约束**：允许可选字段 `description: String`；禁止出现 `enum`、`items`、`properties`、`required`、`additionalProperties`。
* **可空与容器组合**：支持显式可空修饰 `type: ["handle", "null"]`，并可递归作为 `array.items`、`OrderedMap.additionalProperties` 或 `TypedDict.properties` 的子类型。

---

## 三、 静态资源符号表核对与外部边界约束

`TypeSpec` 的词法与结构解析不依赖编译上下文；在进入静态检查阶段后，编译器与宿主对包含 `handle` 的类型附加以下全局存在性、可见性与外部边界可寻址性约束：

1. **全局静态资源符号表 $\Sigma_{\text{res}}$ 与存在性/可见性核验**：
   * 编译器在阶段 1 扫描所有挂载根单元 `MountUnit.resources` 中的 `__ax_resources__.yaml` 头文件，构建全局资源符号表：
     $$\Sigma_{\text{res}} : \text{HandleKind} \rightharpoonup \text{ResourceSpec}$$
   * 对工作流 `input`、`output`、`graph.variables` 及节点 `output` 中递归出现的每一个句柄叶子 `{ type: "handle", kind: "<M>:<C>" }`：
     * **存在性核验**：必须满足 $\texttt{"}\langle M \rangle\texttt{:}\langle C \rangle\texttt{"} \in \text{dom}(\Sigma_{\text{res}})$，否则在对应 `/kind` 路径处报告编译期错误 **`type.unknown_resource`**；
     * **包可见性核验**：设当前工作流标识为 $W \in \text{PackagePath} \cup \{\texttt{"\_\_main\_\_"}\}$，$M$ 必须满足与跨包工作流相同的 `_` 前缀私有段可见性谓词 $\text{Visible}(W, M)$（详见 [`01-cfg-topology-and-sese-validation.md`](../03-static-flow-analysis/01-cfg-topology-and-sese-validation.md) 第四节），否则在对应 `/kind` 路径处报告编译期错误 **`visibility.private_resource`**。
2. **静态资源规格 `ResourceSpec`、公开定位符良构性与 `WorkflowBundle.resources` 裁剪**：
   * 每个 `__ax_resources__.yaml` 文档必须为仅包含顶层键 `resources: Map<PublicClassName, ResourceSpec | null>` 的非空闭合映射（缺失或为空映射报 `schema.resources`，类名非法报 `schema.identifier`，额外字段报 `schema.unknown_field`）；
   * 每个非空资源项 `ResourceSpec ::= { description?: String, locator?: TypeSpec }` 为闭合映射（资源持久性 `persistent` 属于 Worker Python `@resource` 类协议，严禁在静态头文件中重复声明，未知字段报 `schema.unknown_field`）；
   * 若声明了公开 `locator: L`，则 $L$ 必须为满足**顶层非可空**且**纯值无句柄**的良构 `TypeSpec`：
     $$\text{Nullable}(L) = \text{false} \;\land\; \lnot \text{ContainsHandle}(L)$$
     违反上述任一条件时，在 `/resources/<Class>/locator` 处报告编译期错误 **`type.schema`**（`target_kind = "resource"`）。这确保可空句柄 `type: ["handle", "null"]` 的空值唯一由顶层 `null` 表达，且定位符不递归嵌套受管句柄；
   * **可达性裁剪与边界反序列化不变量**：编译产物 `WorkflowBundle { entry, resources, workflows }` 的 `resources` 映射仅保留 `workflows` 依赖闭包内实际引用到的 `HandleKind -> ResourceSpec` 条目；跨语言边界反序列化 `ResourceSpec`（如 `validate_root_interface`、`validate_external_input`、`validate_external_output`）时同步强制校验上述公开 `locator` 非可空纯值不变量。
3. **外部边界公开可寻址约束（Root Interface & `executor: "signal"`）**：
   * **`executor: "signal"` 输出约束**：`signal` 节点的 `input` 仅声明编译期只读定值依赖，允许引用内部私有句柄；但其 `output` 中递归出现的每个句柄叶子 `{ type: "handle", kind: K }` 必须已声明公开定位符（$\Sigma_{\text{res}}(K).\text{locator} \neq \bot$），否则在对应 `/kind` 路径处报告编译期错误 **`type.signal_handle`**；
   * **根工作流接口约束（`validate_root_interface` 与 `"__main__"`）**：具名工作流包允许在顶层 `input` / `output` 中声明内部不可寻址句柄（供 `use: workflow:<PackagePath>` 跨包调用），并在其被选作宿主根入口启动时通过 `validate_root_interface(input, output, resources)` 递归核验顶层 `input` 与 `output` 中的所有句柄均具备公开 `locator`；匿名入口 `"__main__"` 不可被其他工作流调用，编译器直接对 `"__main__"` 的顶层 `input` 与 `output` 执行同一静态检查，违例时在对应 `/kind` 路径处报告编译期错误 **`type.root_handle`**（`target_kind = "variable"`）；
   * **结构校验与访问授权正交**：公开 `locator` 的 `TypeSpec` 校验仅验证名义 `kind` 与定位符的纯值数据结构（具体实例信封兼容性见 [`02-type-equivalence-and-compatibility.md`](./02-type-equivalence-and-compatibility.md)），不隐含跨租户或调用方访问授权；外部传入定位符的归属核验由宿主控制面入口或业务资源类的签名定位符协议负责。
