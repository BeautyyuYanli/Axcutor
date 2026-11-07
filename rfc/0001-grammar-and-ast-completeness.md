# RFC-0001: AST 子结构文法补全、参数映射消歧与拓扑约束修正

* **状态**：Implemented
* **所属分类**：形式化规范完备性与自洽性修正（Formal Specification Soundness & Completeness）
* **核心解决的问题**：补全 `GraphSpec` / `ForEachSpec` / `RetrySpec` / `CatchSpec` 形式化文法；修复 `use.with.output` 默认映射污染与 `for_each` 交互歧义；修正 `ForkJoinEdge` 自身端点互斥与跨文件递归调用契约。
* **关联规范模块**：
  * [`01-grammar-and-ast/01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md)
  * [`01-grammar-and-ast/02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md)
  * [`01-grammar-and-ast/03-edge-and-cfg-grammar.md`](../manual/spec/01-grammar-and-ast/03-edge-and-cfg-grammar.md)
  * [`03-static-flow-analysis/01-cfg-topology-and-sese-validation.md`](../manual/spec/03-static-flow-analysis/01-cfg-topology-and-sese-validation.md)

---

## 一、 问题背景与缺陷分析

### 问题 1.1：核心修饰符与容器 AST（`GraphSpec`、`ForEachSpec`、`RetrySpec`、`CatchSpec`）在 `spec/` 中缺失形式化定义
在 [`01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md) 的节点字段表中，直接引用了 `GraphSpec`、`ForEachSpec`、`RetrySpec`、`CatchSpec` 四个 AST 类型名，但在整个 `manual/spec/` 目录下并未给出它们的字段文法与编译期良构性公理（部分约束仅以非形式化速查表形式散落在 `manual/docs/reference/01-node-and-graph-fields.md` 中）：

1. **`GraphSpec`**：未在 `spec/` 中规定 `nodes` 必须为非空映射、`edges` 必须为非空数组、`max_steps` 必须为正整数（$\ge 1$），以及 `variables` 声明中显式带类型形态 `{ <TypeSpec>, default: <literal> }` 与字面量简写形态 `<literal>` 的语法消歧准则。
2. **`ForEachSpec`**：
   * 未在 `spec/` 中规定 `source` 的静态类型必须为非空 `array` 或非空 `OrderedMap`（`Nullable(TypeOf(source)) == false`）；
   * 未在 `spec/` 中规定 `key` 仅当 `source` 为 `OrderedMap` 时允许声明，当 `source` 为 `array` 时声明 `key` 必须报编译期错误；
   * 未在 `spec/` 中规定 `filter` 与 `collect` 不可全部省略（若 `filter`、`collect`、`state` 三者同时为空，则该 `for_each` 无任何数据出口）；
   * 未在 `spec/` 中规定 `filter.<branch>` 的每个键 `<branch>` 必须属于 $\text{dom}(N.\text{branches}) \cup \text{dom}(N.\text{catch})$；
   * 未在 `spec/` 中规定 `collect.<field>` 的每个键 `<field>` 必须属于节点单次迭代产出字段集且不属于 $\text{set}(N.\text{for\_each.state})$。
3. **`RetrySpec` 与 `CatchSpec`**：
   * 未在 `spec/` 中规定 `retry.max_attempts` 必须为 $\ge 2$ 的整数、`retry.backoff_ms` 必须为 $\ge 0$ 的整数；
   * 未在 `spec/` 中规定当节点为 `ActionNode`（`run`）或 `RootRunNode` 时，`retry.on` 与 `catch` 的选择器集合中**禁止包含 `max_steps`**（因原子 `run` 节点不存在内部图步数计数器，`max_steps` 永远不可能触发，属于死分支/无效配置）。

---

### 问题 1.2：`use.with` 默认映射规则不完整、未用输出字段强制污染调用方，且与 `for_each` 组合时语义未定义
在 [`02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md) 中：

1. **`with.input` 默认同名匹配未写入文法规范**：
   `02-binding-and-projection-grammar.md` 第 三.1 节仅定义了显式写出 `with.input.<param>` 时的四态解析规则，未形式化定义“省略 `with.input.<param>`（或完全省略 `with`）时，对 `use: workflow:<path>` 默认绑定调用方当前作用域的同名顶层变量 `<param>`”这一核心规则，也未说明 `use: builtin:<op>` 是否允许省略 `with.input` / `with.output`。
2. **`with.output` 省略字段强制恒等写回导致命名空间污染**：
   第 三.2 节规定：“省略 `with.output`（或省略某个输出字段）时，默认按恒等映射 `<out_field>: <out_field>` 处理。”
   若被调用子工作流声明了 `output: { result: { type: string }, debug_info: { type: string } }`，而调用方只关心 `result`：
   ```yaml
   call_sub:
     use: workflow:common/process
     with:
       output:
         result: final_result
   ```
   按照现行规范，`debug_info` 会被强制按原名 `debug_info` 注入调用方图的符号表 $\Sigma_{\text{caller}}$ 并在运行期写回！如果调用方图中恰好已有一个名为 `debug_info` 但类型为 `integer` 或 `array` 的局部变量，将直接触发“同帧同名同类型不变式”编译错误；即便类型相同，也会在运行期静默覆盖调用方原有的 `debug_info` 变量。同时，语言中没有任何语法可以丢弃（Ignore/Discard）不需要的子工作流输出字段。
3. **`use: workflow:<path>` 挂载 `for_each` 时的输出映射歧义**：
   `CallNode` 禁止声明节点级 `output`，其单次调用输出由被调用工作流的 `output` 与 `with.output` 决定。当 `CallNode` 同时挂载 `for_each` 时：
   * `for_each.collect` 和 `for_each.state` 引用的字段名是被调用工作流的原始 `output` 字段名，还是经 `with.output` 重命名后的字段名？
   * 若不允许在挂载 `for_each` 的 `CallNode` 上写 `with.output`（或规定由 `for_each.collect` 直接映射被调用方的原始 `output` 字段名），现行规范完全没有界定。

---

### 问题 1.3：`ForkJoinEdge` 并发组成员的独占拓扑公理遗漏了“自身所在三元边”
在 [`01-cfg-topology-and-sese-validation.md`](../manual/spec/03-static-flow-analysis/01-cfg-topology-and-sese-validation.md) 第 二.1 节中规定：
> “对于出现在任意三元并发边 `src -> [W_1, ..., W_k] -> join_target` 中的每个成员节点 $W_i$：$W_i$ 禁止在**其他任何边**中作为 `SourceEndpoint`、`TargetEndpoint`、`JoinTarget` 或再次出现在其他 `ConcurrentGroup` 中。”

“其他任何边”这一措辞遗漏了**当前这条三元边自身**的 `src` 与 `join_target`。例如：
* `w1 -> [w1, w2] -> next_node`
* `prev_node -> [w1, w2] -> w1`

若图仅包含这样一条三元边，则 `w1` 并未出现在“其他任何边”中，从而绕过了字面规则并破坏 SESE 拓扑。

---

### 问题 1.4：跨文件调用图（Workflow Call Graph）的递归合法性未定义
* [`03-guardrails-and-step-accounting.md`](../manual/spec/04-runtime-execution-model/03-guardrails-and-step-accounting.md) 定义了运行期 `MAX_CALL_DEPTH`（默认 64）与 `StackOverflowError`；由于单文件内联 `graph` 的语法嵌套深度是有限静态常数，运行期唯一可能超过 64 层调用深度的场景是 `use: workflow:<path>` 发生直接或间接递归调用。
* 但 `spec/` 未明确规定：编译器在静态检查 `use: workflow:<path>` 时，是否允许工作流调用图存在环路（依靠顶层 `input`/`output`/`branches` 签名解耦编译，并在运行期由 `MAX_CALL_DEPTH` 兜底），还是静态禁止递归。

---

### 问题 1.5：`run` 内部 `variables.<FieldPathExpr>` 的提取边界与 `RootRunNode` 适配缺失
* [`02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md) 第 二 节规定：“`run` 内部变量引用 `variables.` + `FieldPathExpr`：其根变量 $\text{RootVar}(E)$ 必须已显式包含在所属 `run` 节点的 `input: [...]` 列表中”。
* 两处疏漏：
  1. 当根节点本身为 `RootRunNode` 时，其 `input` 是映射 `Map<Identifier, TypeSpec>` 而非列表 `List<Identifier>`，此时根变量应校验 $\text{RootVar}(E) \in \text{dom}(\text{root.input})$；
  2. 当 `run` 为自然语言字符串时，形如 `"读取 variables.user.name."` 末尾紧跟英文句号 `.`，若无明确的正则词法最长匹配规则，句尾句号会被误解析为空的下一级字段而报语法错误。

---

## 二、 解决方案与规范修订设计

### 方案 2.1：在 `01-grammar-and-ast/01-document-and-node-grammar.md` 中补全四类子结构形式化规范

新增第四节与第五节，形式化定义 `GraphSpec`、`ForEachSpec`、`RetrySpec`、`CatchSpec`：

1. **`GraphSpec` 良构性公理**：
   * `nodes: Map<NodeId, InnerNode>`：必填，且 $\text{dom}(\text{nodes}) \neq \emptyset$。
   * `edges: List<EdgeExpr>`：必填，且 $|\text{edges}| \ge 1$。
   * `max_steps?: PositiveInt`：可选正整数（$\ge 1$），默认值为 `1000`。
   * `variables?: Map<Identifier, VarDecl>`：可选映射。对每个条目 `<v>: <val>`：
     * 若 `<val>` 为映射对象且包含键 `type` 与 `default`（且除 `TypeSpec` 允许的字段与 `default` 外无其他键），解析为**显式带类型声明** `(TypeSpec, default_literal)`；
     * 否则，`<val>` 解析为**字面量简写声明** `shorthand_literal`（注意：这要求任何直接作为 `variables` 初值的 `TypedDict` 或 `OrderedMap` 对象字面量必须采用显式带类型声明 `{ type: object, ..., default: {...} }`，避免与带 `type` 键的业务字典混淆）。

2. **`ForEachSpec` 良构性公理**：
   * `source: FieldPathExpr`（必填）：令 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{source})$，必须满足 $\text{Nullable}(T_{\text{src}}) = \texttt{false}$ 且 $\text{BaseType}(T_{\text{src}}) \in \{\texttt{array}, \texttt{object (OrderedMap)}\}$。
   * `item: Identifier`（必填）。
   * `key?: Identifier`（可选）：**仅当 $T_{\text{src}}$ 为 `OrderedMap` 时允许声明**；若 $\text{BaseType}(T_{\text{src}}) = \texttt{array}$ 且声明了 `key`，编译期报错。
   * `index?: Identifier`（可选）。
   * `concurrency?: PositiveInt`（可选，默认 `1`）。
   * `filter?: Map<BranchOrCatchId, Identifier>`（可选）：每个键 $b \in \text{dom}(\text{filter})$ 必须满足 $b \in \text{dom}(N.\text{branches}) \cup \text{dom}(N.\text{catch})$。
   * `collect?: Map<Identifier, Identifier | { into: Identifier, flatten: Boolean }>`（可选）：每个键 $f \in \text{dom}(\text{collect})$ 必须属于节点 $N$ 的单次迭代输出字段集 $\text{dom}(\text{SingleOut}(N))$，且 $f \notin \text{set}(N.\text{for\_each.state})$。当指定 `flatten: true` 时，必须满足 $\text{BaseType}(T_{\text{src}}) = \texttt{array}$ 且 $\text{BaseType}(\text{SingleOut}(N)[f]) = \texttt{array}$ 且 $\text{Nullable}(\text{SingleOut}(N)[f]) = \texttt{false}$。
   * `state?: List<Identifier>`（可选）：必须为无重复元素的标识符列表，且 $\text{set}(\text{state}) \subseteq \text{dom}(\text{SingleOut}(N))$。

3. **`RetrySpec` 与 `CatchSpec` 良构性公理**：
   * `retry.max_attempts: Int`（必填）：必须满足 $\text{max\_attempts} \ge 2$。
   * `retry.backoff_ms?: NonNegativeInt`（可选，默认 `0`）：必须满足 $\text{backoff\_ms} \ge 0$。
   * `retry.on?: List<RecoverableSelector>`（可选，默认 `["execution", "timeout"]`）：元素无重复，且属于 `{"execution", "timeout", "contract", "max_steps"}`。**当所属节点为 `ActionNode` 或 `RootRunNode` 时，禁止包含 `"max_steps"`**。
   * `catch: Map<CatchBranchId, List<RecoverableSelector> | "*">`：
     * 每个 `<CatchBranchId>` 必须为合法 `Identifier`，禁止为 `start`、`end`、`fail`，且 $\text{dom}(N.\text{catch}) \cap \text{dom}(N.\text{branches}) = \emptyset$；
     * **当所属节点为 `ActionNode` 时，任何分支的显式选择器列表中禁止包含 `"max_steps"`**（通配符 `"*"` 在 `ActionNode` 上展开为 `{"execution", "timeout", "contract"}`）。

---

### 方案 2.2：修正 `use.with` 映射规范与 `for_each` 交互契约

在 [`02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md) 中：

1. **明确区分 `use: workflow:<path>` 与 `use: builtin:<op>` 的省略规则**：
   * **`use: builtin:<op>`**：`with.input` 与 `with.output` 中的必填参数名（如 `source`、`value`、`result`、`item`、`rest`）**必须显式声明**，不支持隐式同名省略。
   * **`use: workflow:<path>` 的 `with.input` 默认同名规则**：对于目标工作流 `target.input` 中声明的每个参数 $p$，若未在 `with.input` 中显式出现，编译器自动补全为同名顶层变量引用 `with.input.p = p`。
2. **在 `use.with.output` 中引入显式丢弃符 `null`（或 `"_"`）并明确部分映射规则**：
   * 当完全省略 `with.output` 时，对目标工作流的所有输出字段 $f \in \text{dom}(\text{target.output})$ 默认执行恒等映射 $f \mapsto f$；
   * 支持将不需要的目标输出字段映射到 `null`（或保留字 `"_"`）以显式丢弃该输出，被丢弃的字段不进入调用方符号表 $\Sigma_{\text{caller}}$，也不参与单射冲突检查：
     ```yaml
     call_sub:
       use: workflow:common/process
       with:
         output:
           result: final_result
           debug_info: null   # 显式丢弃 debug_info，不写回当前图 variables
     ```
3. **挂载 `for_each` 的 `CallNode` 输出映射规则**：
   * 当 `CallNode`（`use: workflow:<path>`）挂载了 `for_each` 时，**禁止声明 `with.output`**；
   * 单次迭代产出直接采用被调用工作流顶层声明的 `output` 字段名，由 `for_each.collect` 与 `for_each.state` 负责将其提升或折叠至外层图变量（对于 `for_each.state: [s]`，要求被调用工作流 `output` 中包含同名同类型的字段 `s`，或允许在 `for_each.state` 中书写映射 `{ <callee_out_field>: <outer_state_var> }`）。

---

### 方案 2.3：修正 `ForkJoinEdge` 独占拓扑与调用图递归契约

1. **修正 `ForkJoinEdge` 独占拓扑公式**（[`01-cfg-topology-and-sese-validation.md`](../manual/spec/03-static-flow-analysis/01-cfg-topology-and-sese-validation.md)）：
   * 设并发边为 $e = (\text{src} \to [W_1, \dots, W_k] \to \text{join\_target})$。对每个 $W_i \in \{W_1, \dots, W_k\}$：
     * $W_i \neq \text{NodeOf}(\text{src})$ 且 $W_i \neq \text{join\_target}$（禁止与本条并发边的源节点或汇合节点重合）；
     * 在图 $G$ 的其余所有边 $e' \in G.\text{edges} \setminus \{e\}$ 中，$W_i$ 禁止出现在任何位置（源、目标、并发组成员、汇合点）。
2. **明确跨文件调用图（`use: workflow:<path>`）支持受护栏保护的递归**：
   * 编译期基于各工作流文件顶层的静态签名（`input`、`output`、`branches`）进行模块化独立类型检查，允许跨文件调用图存在直接或间接递归环路；
   * 运行期由 [`03-guardrails-and-step-accounting.md`](../manual/spec/04-runtime-execution-model/03-guardrails-and-step-accounting.md) 的 `MAX_CALL_DEPTH`（默认 64）强制保证有界终止。
3. **`run` 字符串中的变量提取词法规则**：
   * 在字符串类型的 `run` 中，静态检查器按正则模式 `\bvariables\.[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*` 提取所有 `FieldPathExpr`（句尾紧跟的标点 `.` 如 `variables.user.name.` 因不匹配后续 `[a-zA-Z_]`，自动作为普通标点剥离）。
   * 当节点为 `RootRunNode` 时，根变量校验条件为 $\text{RootVar}(E) \in \text{dom}(\text{root.input})$。
