# 01. 文档根结构与三类节点文法规范

本规范定义 Axcutor 工作流文档的顶层 AST 结构、图内三类节点（`run`、`graph`、`use`）的形态鉴别准则、分支关联输出契约（`BranchSpec`），以及控制流图容器（`GraphSpec`）与节点修饰符子结构（`ForEachSpec`、`RetrySpec`、`CatchSpec`）的形式化良构条件。

---

## 一、 顶层根节点（`RootNode`）文法

每个工作流文件反序列化后必须是一个映射对象（`RootNode`），满足以下文法约束：

1. **必填契约字段**：
   * `input: Map<Identifier, TypeSpec>`（可为空映射 `{}`）
   * `output: Map<Identifier, TypeSpec>`（可为空映射 `{}`）
2. **可选契约字段**：
   * `branches: Map<BranchId, BranchSpec>`（默认 `{}`）
3. **执行体互斥鉴别（Mutual Exclusion）**：
   * `RootNode` **必须且只能在 `run` 与 `graph` 两个键中声明其一**：
     * **当包含 `run`（`RootRunNode`）**：允许额外声明 `timeout_ms?: PositiveInt` 与 `retry?: RetrySpec`；禁止声明 `for_each`、`catch`、`with`、`variables`、`nodes`、`edges`、`max_steps`。
     * **当包含 `graph`（`RootGraphNode`）**：禁止在顶层声明 `timeout_ms`、`retry`、`for_each`、`catch`、`with`。

---

## 二、 图内节点（`InnerNode`）的三态鉴别与字段约束

在 `graph.nodes: Map<NodeId, InnerNode>` 中，每个键 `<NodeId>` 必须为合法标识符，且**禁止使用保留字 `start`、`end`、`fail`**。

每个 `InnerNode` 必须通过鉴别键 **`run`、`graph`、`use` 三选一** 唯一确定其 AST 子类型：

| AST 节点类型 | 唯一鉴别键 | 允许的可选字段 | 禁止出现的字段 |
| :--- | :--- | :--- | :--- |
| **`ActionNode`** | `run` | `input: List<Identifier>`<br>`output: Map<Identifier, TypeSpec>`<br>`branches: Map<BranchId, BranchSpec>`<br>`for_each: ForEachSpec`<br>`timeout_ms: PositiveInt`<br>`retry: RetrySpec`<br>`catch: CatchSpec` | `with`, `graph`, `use` |
| **`InlineGraphNode`** | `graph` | `input: List<Identifier>`<br>`output: List<Identifier> \| Map<Identifier, TypeSpec>`<br>`branches: Map<BranchId, BranchSpec>`<br>`for_each: ForEachSpec`<br>`timeout_ms: PositiveInt`<br>`retry: RetrySpec`<br>`catch: CatchSpec` | `with`, `run`, `use` |
| **`CallNode`** | `use` | `with: WithSpec`<br>*(以下仅当 `use` 为 `workflow:<path>` 时允许，为 `builtin:<op>` 时禁止)*：<br>`for_each: ForEachSpec`<br>`timeout_ms: PositiveInt`<br>`retry: RetrySpec`<br>`catch: CatchSpec` | `input`, `output`, `branches`, `run`, `graph` |

* **`input` 列表去重约束**：在 `ActionNode` 与 `InlineGraphNode` 中，`input` 若声明，必须为无重复元素的顶层变量标识符列表 `List<Identifier>`。
* **`InlineGraphNode.output` 列表简写去重约束**：当 `InlineGraphNode.output` 为 `List<Identifier>` 时，列表元素必须为无重复的标识符（等价于从子图内部符号表推导对应 `TypeSpec`）。
* **`CallNode` 挂载 `for_each` 时的 `with.output` 互斥约束**：当 `CallNode`（`use: workflow:<path>`）声明了 `for_each` 时，禁止在 `with` 中声明 `output`（单次迭代输出直接由 `for_each.collect` 与 `for_each.state` 对接被调用工作流的 `output` 字段）。

---

## 三、 分支关联输出契约（`BranchSpec`）规范化

在 `RootNode`、`ActionNode` 或 `InlineGraphNode` 的 `branches: Map<BranchId, BranchSpec>` 中：

* 每个 `<BranchId>` 必须为合法标识符，不可为保留字 `start`、`end`、`fail`，且不可与同节点 `catch` 中的异常分支名重复。
* 每个 `BranchSpec` 在 AST 规范化阶段统一消糖为二元组 `(description: String, output_subset: Set<Identifier>)`：
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
4. **`variables?: Map<Identifier, VarDecl>`（可选，默认 `{}`）**：
   * 每个键 `<v>` 必须为合法 `Identifier`。解析器按以下确定性准则对 `<val>` 进行形态消歧：
     * **显式带类型声明（`ExplicitVarDecl`）**：若 `<val>` 为映射对象且同时包含键 `type` 与 `default`（且除 `TypeSpec` 允许的契约字段与 `default` 外不包含其他键），则将其拆分为 `(TypeSpec, default_literal)`；
     * **字面量简写声明（`ShorthandVarDecl`）**：否则，将 `<val>` 整体解析为初始字面量 `shorthand_literal`（注：任何直接作为 `variables` 初值的 `TypedDict` 或 `OrderedMap` 对象字面量必须采用显式带类型声明 `{ type: object, ..., default: {...} }`，见 [`03-two-phase-symbol-resolution.md`](../02-type-system-and-symbols/03-two-phase-symbol-resolution.md)）。

---

## 五、 节点修饰符子结构（`ForEachSpec`、`RetrySpec`、`CatchSpec`）良构性公理

### 1. 集合遍历修饰符（`ForEachSpec`）

当 `ActionNode`、`InlineGraphNode` 或 `CallNode`（仅限 `use: workflow:<path>`）声明 `for_each` 时，其值必须为满足以下良构性公理的映射对象：

* **`source: FieldPathExpr`（必填）**：
  * 令 $T_{\text{src}} = \text{TypeOf}(\Sigma_{\text{outer}}, \text{source})$。必须满足**非空集合约束**：
    $$\text{Nullable}(T_{\text{src}}) = \texttt{false} \;\land\; \text{BaseType}(T_{\text{src}}) \in \{\texttt{array}, \texttt{object (OrderedMap)}\}$$
    若 $T_{\text{src}}$ 可空（含 `null`）或为固定属性结构体（`TypedDict`）或标量类型，编译期报错。
* **`item: Identifier`（必填）**：注入单次迭代局部帧的元素（当源为 `array` 时）或值（当源为 `OrderedMap` 时）变量名。
* **`key?: Identifier`（可选）**：
  * **仅当 $T_{\text{src}}$ 为 `OrderedMap`（`type: object` + `additionalProperties`）时允许声明**；
  * 若 $\text{BaseType}(T_{\text{src}}) = \texttt{array}$ 且显式声明了 `key`，编译期报错。
* **`index?: Identifier`（可选）**：注入单次迭代序号（`0, 1, ...`，类型 `integer`）的变量名。
* **`concurrency?: PositiveInt`（可选，默认 `1`）**：
  * 必须为正整数（$\text{concurrency} \ge 1$）；当声明了非空 `state` 时，必须满足 $\text{concurrency} = 1$。
* **三通道数据出口与键存在性公理**：
  * 记节点 $N$ 的**单次迭代输出字段契约表 $\text{SingleOut}(N)$** 为：
    * 当 $N$ 为 `ActionNode` 或 `InlineGraphNode` 时，$\text{SingleOut}(N) = N.\text{output}$（若 `InlineGraphNode.output` 为列表简写，则为其关联的子图符号表类型映射）；
    * 当 $N$ 为 `CallNode`（`use: workflow:<path>`）时，$\text{SingleOut}(N) = \text{target.output}$（被调用工作流顶层声明的 `output` 映射）。
  * **数据出口非空约束**：`filter`、`collect`、`state` 三者不可同时为空：
    $$\text{dom}(\text{filter}) \cup \text{dom}(\text{collect}) \cup \text{set}(\text{state}) \neq \emptyset$$
  * **`filter?: Map<BranchOrCatchId, Identifier>`（可选，默认 `{}`）**：
    * 每个键 $b \in \text{dom}(\text{filter})$ 必须为当前节点已声明的正常分支或异常捕获分支：
      $$\forall b \in \text{dom}(\text{filter}), \quad b \in \text{dom}(N.\text{branches}) \cup \text{dom}(N.\text{catch})$$
      *(注：对 `CallNode` 而言，$N.\text{branches}$ 即被调用工作流顶层声明的 `target.branches`)*。
    * 每个值 `<target_var>` 必须为合法顶层 `Identifier`。
  * **`collect?: Map<Identifier, Identifier | { into: Identifier, flatten?: Boolean }>`（可选，默认 `{}`）**：
    * 每个键 $f \in \text{dom}(\text{collect})$ 必须属于节点单次迭代输出字段集，且不得与 `state` 变量重叠：
      $$\forall f \in \text{dom}(\text{collect}), \quad f \in \text{dom}(\text{SingleOut}(N)) \;\land\; f \notin \text{set}(N.\text{for\_each.state})$$
    * 当值采用 `{ into: <target_var>, flatten: true }` 时，必须同时满足：
      $$\text{BaseType}(T_{\text{src}}) = \texttt{array} \;\land\; \text{BaseType}(\text{SingleOut}(N)[f]) = \texttt{array} \;\land\; \text{Nullable}(\text{SingleOut}(N)[f]) = \texttt{false}$$
  * **`state?: List<Identifier>`（可选，默认 `[]`）**：
    * 必须为无重复元素的顶层 `Identifier` 列表，且每个状态变量必须属于节点单次迭代输出字段集：
      $$\text{set}(\text{state}) \subseteq \text{dom}(\text{SingleOut}(N))$$
    * **多分支下的 `state` 完备性约束**（详见 [`03-branch-sensitive-definite-assignment.md`](../03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)）：若节点 $N$ 同时声明了非空 `branches`（对 `CallNode` 指 `target.branches`），则每一个正常业务分支 $b \in \text{dom}(N.\text{branches})$ 承诺产出的字段子集均必须包含全部 `state` 变量：
      $$\forall b \in \text{dom}(N.\text{branches}), \quad \text{set}(\text{state}) \subseteq \text{set}(N.\text{branches}[b].\text{output})$$

### 2. 原地快照重试修饰符（`RetrySpec`）

当 `RootRunNode`、`ActionNode`、`InlineGraphNode` 或 `CallNode`（仅限 `use: workflow:<path>`）声明 `retry` 时，其值必须为满足以下良构性公理的映射对象：

* **`max_attempts: Int`（必填）**：必须为 $\ge 2$ 的整数（含首次执行在内至少允许 2 次尝试）。
* **`backoff_ms?: NonNegativeInt`（可选，默认 `0`）**：必须为 $\ge 0$ 的整数。
* **`on?: List<RecoverableSelector>`（可选，默认 `["execution", "timeout"]`）**：
  * 必须为非空且无重复元素的列表，其中每个元素必须属于可恢复异常选择器全集：
    $$\mathcal{S}_{\text{all}} = \{\texttt{"execution"}, \texttt{"timeout"}, \texttt{"contract"}, \texttt{"max\_steps"}\}$$
  * **原子 `run` 节点禁止包含 `"max_steps"`**：当所属节点为 `ActionNode` 或 `RootRunNode` 时，因原子 `run` 执行体不存在图步数计数器，必须满足：
    $$\texttt{"max\_steps"} \notin \text{set}(\text{retry.on})$$
    否则编译期报错。

### 3. 异常捕获修饰符（`CatchSpec`）

当 `ActionNode`、`InlineGraphNode` 或 `CallNode`（仅限 `use: workflow:<path>`）声明 `catch` 时，其值必须为非空映射 `Map<CatchBranchId, List<RecoverableSelector> | "*">`，且满足以下良构性公理：

* **分支名合法性与互斥约束**：
  * 每个键 $c \in \text{dom}(N.\text{catch})$ 必须为合法 `Identifier`，禁止为保留字 `start`、`end`、`fail`；
  * 禁止与同一节点的正常业务分支重名：$\text{dom}(N.\text{catch}) \cap \text{dom}(N.\text{branches}) = \emptyset$。
* **选择器完备与互斥约束**：
  * 定义节点 $N$ 允许捕获的选择器论域 $\mathcal{S}(N)$：
    * 若 $N$ 为 `ActionNode`（`run`）：$\mathcal{S}(N) = \{\texttt{"execution"}, \texttt{"timeout"}, \texttt{"contract"}\}$（**显式列表中禁止包含 `"max_steps"`**）；
    * 若 $N$ 为 `InlineGraphNode`（`graph`）或 `CallNode`（`use: workflow:<path>`）：$\mathcal{S}(N) = \{\texttt{"execution"}, \texttt{"timeout"}, \texttt{"contract"}, \texttt{"max\_steps"}\}$。
  * 对每个显式列表分支 $c$，其值必须为 $\mathcal{S}(N)$ 的非空无重复子集，且不同分支的显式选择器集合之间必须两两无交集；
  * 同一 `catch` 块中最多允许 **1 个**分支将值设为通配符 `"*"`，其在 AST 规范化阶段展开为剩余可恢复异常集合 $\mathcal{S}(N) \setminus \bigcup_{c' \neq c} \text{set}(N.\text{catch}[c'])$（展开后若为空集则编译期报错）。
