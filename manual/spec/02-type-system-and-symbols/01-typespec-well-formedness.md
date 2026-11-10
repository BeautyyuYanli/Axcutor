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

## 三、 静态资源符号表核对与纯值边界约束

`TypeSpec` 的词法与结构解析不依赖编译上下文；在进入静态检查阶段后，编译器与宿主对包含 `handle` 的类型附加以下全局存在性、可见性与边界约束：

1. **全局静态资源符号表 $\Sigma_{\text{res}}$ 与存在性/可见性核验**：
   * 编译器在阶段 1 扫描所有挂载根单元 `MountUnit.resources` 中的 `__ax_resources__.yaml` 头文件，构建全局资源符号表：
     $$\Sigma_{\text{res}} = \{ \texttt{"}\langle\text{ModulePath}\rangle\texttt{:}\langle\text{PublicClassName}\rangle\texttt{"} \}$$
   * 对工作流 `input`、`output`、`graph.variables` 及节点 `output` 中递归出现的每一个句柄类型 `{ type: "handle", kind: "<M>:<C>" }`：
     * **存在性核验**：必须满足 $\texttt{"}\langle M \rangle\texttt{:}\langle C \rangle\texttt{"} \in \Sigma_{\text{res}}$，否则在对应 `TypeSpec` 路径处报告编译期错误 **`type.unknown_resource`**；
     * **包可见性核验**：设当前工作流标识为 $W \in \text{PackagePath} \cup \{\texttt{"\_\_main\_\_"}\}$，$M$ 必须满足与跨包工作流相同的 `_` 前缀私有段可见性谓词 $\text{Visible}(W, M)$（详见 [`01-cfg-topology-and-sese-validation.md`](../03-static-flow-analysis/01-cfg-topology-and-sese-validation.md) 第四节），否则报告编译期错误 **`visibility.private_resource`**。
2. **纯 JSON 值边界（Pure-Value Boundary）**：
   * **协作式等待节点（`executor: "signal"`）输出限制**：`signal` 节点的输出由外部调用方通过 HTTP/CLI 直接提供纯 JSON 负载，外部调用方不持有运行时资源注册表授权。因此当 `run.executor == "signal"` 时，其 `output` 中任何字段的 `TypeSpec` 严禁包含或递归嵌套 `handle`，否则在编译期报告 **`type.signal_handle`**（`signal` 节点的 `input` 仅用于编译期定值依赖分析，允许引用句柄变量）。
   * **宿主根执行入口（Root Ingress / Egress）限制**：可复用的具名工作流包允许在顶层 `input` 与 `output` 中声明含 `handle` 的类型（供其他工作流通过 `use: workflow:<PackagePath>` 在内部传递句柄，或通过 `axcutor contracts` 生成契约桩）；但当某工作流作为根执行入口启动时，宿主与运行时在创建根执行前递归检查入口工作流的顶层 `input` 与 `output`，若包含 `handle`（含可空句柄或容器内嵌句柄）则直接拒绝启动。
