# RFC-0007: 移除四项隐式类型推导与绑定语法糖，收敛为单遍显式符号表与模块化类型检查

* **状态**：Proposed
* **所属分类**：类型系统简化与模块化完备性（Type System Simplification & Modular Soundness）
* **核心解决的问题**：
  1. 移除 `graph.variables` 的无类型字面量简写（`ShorthandVarDecl`），统一为携带可选 `default` 的显式 `TypeSpec`（同时支持声明带初值局部变量与未初始化局部变量），消除对象字面量词法消歧悖论、阶段 1 `WrittenVars` 孤立标量预扫描与阶段 3 后置逆向字面量校验；
  2. 移除 `InlineGraphNode.output` 的列表简写（`output: [v_1, v_2]`），统一为显式 `Map<Identifier, TypeSpec>`，解除父子图符号表构建的跨层递归交织；
  3. 确立**显式符号表原则（Declaration-Before-Use）**，要求图内所有变量均由显式类型契约源声明，彻底废除三步工作队列不动点推导算法（Worklist Symbol Resolution），将其降维简化为**单遍 $O(N)$ 静态符号收集 + 无状态局部类型检查**，根除 `builtin:assign` 与 `builtin:object_merge` 正反向双模推导在工作队列中的循环等待死锁漏洞；
  4. 移除 `use: workflow:<path>` 中 `with.input` 与 `with.output` 的隐式全量同名自动补全及 `f: null` 显式丢弃语法，使未列出的输出字段默认忽略，解决“脆弱被调用方问题（Fragile Callee Problem）”并实现单文件自包含的静态数据流读写分析。
* **关联规范模块（`manual/spec/`）**：
  * [`index.md`](../manual/spec/index.md)
  * [`01-grammar-and-ast/01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md)
  * [`01-grammar-and-ast/02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md)
  * [`02-type-system-and-symbols/03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md)
  * [`02-type-system-and-symbols/05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md)
  * [`03-static-flow-analysis/02-scoping-and-collision-rules.md`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md)
  * [`03-static-flow-analysis/03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)
  * [`04-runtime-execution-model/01-frame-memory-model.md`](../manual/spec/04-runtime-execution-model/01-frame-memory-model.md)
* **关联文档模块（`manual/docs/`）**：
  * [`guide/01-foundations/01-basics.md`](../manual/docs/guide/01-foundations/01-basics.md)
  * [`guide/01-foundations/02-use-subworkflows.md`](../manual/docs/guide/01-foundations/02-use-subworkflows.md)
  * [`guide/01-foundations/03-inline-graphs.md`](../manual/docs/guide/01-foundations/03-inline-graphs.md)
  * [`guide/04-state-and-resilience/01-frames-and-scoping.md`](../manual/docs/guide/04-state-and-resilience/01-frames-and-scoping.md)
  * [`reference/01-node-and-graph-fields.md`](../manual/docs/reference/01-node-and-graph-fields.md)
  * [`reference/builtins/assign.md`](../manual/docs/reference/builtins/assign.md)
  * [`reference/builtins/object-merge.md`](../manual/docs/reference/builtins/object-merge.md)

---

## 一、 问题背景与现行语法糖引发的架构缺陷分析

现行规范为了允许用户在编写 DSL 时省略部分类型标注与参数绑定，引入了四项隐式语法糖。然而，这四项语法糖在编译器前端（阶段 01 ~ 03）引发了严重的**算法复杂度膨胀、跨阶段耦合以及形式化边界死锁漏洞**。

---

### 痛点 7.1：`graph.variables` 的无类型字面量简写（`ShorthandVarDecl`）引发三阶段割裂与词法消歧悖论

在现行 [`01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md#L54-L58) 与 [`03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md#L27-L91) 中，`graph.variables` 允许同时混写显式带类型声明（`ExplicitVarDecl`：`{ type: ..., default: ... }`）与无类型字面量简写（`ShorthandVarDecl`：`count: 0`、`items: []`、`cur: null`）。

为了支持这一简写，编译器被迫付出了三重高昂代价：

1. **阶段 1 的全图 `WrittenVars(G)` 预扫描**：
   * 若用户在 `variables` 中写了 `status: "pending"`，且后续某节点产出 `type: string, enum: ["pending", "done"]` 写入 `status`，编译器在阶段 1 **绝对不能**将 `status: "pending"` 直接推导为无 `enum` 的普通 `{ type: string }`，否则在后续合并节点输出类型时会因 `{ type: string } \not\equiv { type: string, enum: [...] }` 而误报同帧类型冲突。
   * 因此，阶段 1 被迫先扫描全图所有节点的写入集合 $\text{WrittenVars}(G)$，仅对 $v \notin \text{WrittenVars}(G)$ 的“孤立标量字面量”执行预推导。
2. **阶段 3 的后置逆向字面量校验**：
   * 对于 `items: []` 或 `val: null` 等无法独立确定类型的空容器/空值简写，阶段 1 必须跳过它们，等到阶段 2 靠图内其他节点把 `items` 或 `val` 的类型推导进 $\Sigma_G$ 后，再在阶段 3 回过头来校验 `[] \models \Sigma_G(\text{items})$。
3. **`ExplicitVarDecl` 与 `ShorthandVarDecl` 的语法消歧自相矛盾**：
   * [`01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md#L56-L58) 规定通过“检查映射对象是否同时包含键 `type` 与 `default`”来区分显式类型声明与简写字面量；但 [`03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md#L86-L87) 阶段 3 又允许已存在于 $\text{dom}(\Sigma_G)$（例如已在 `root.output` 中声明为 `TypedDict` 或 `OrderedMap`）的变量使用 `<v>: <literal>` 简写。
   * 一旦该 `TypedDict` 或 `OrderedMap` 的业务初始值对象恰好包含名为 `type` 和 `default` 的业务属性，语法解析器就会将其误判为 `ExplicitVarDecl`！

---

### 痛点 7.2：`InlineGraphNode.output` 列表简写打破父子图模块化边界

在现行 [`01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md#L27) 中，`RootNode.output` 与 `ActionNode.output` 均强制要求显式声明 `Map<Identifier, TypeSpec>`，唯独 `InlineGraphNode.output` 允许写成无类型的列表简写 `output: [v_1, v_2]`。

这导致：
* **父图符号表无法独立构建**：父图为了知道 `v_1, v_2` 的类型，必须在阶段 2 工作队列中挂起等待该内联子图的全部 `input` 变量在父图中推导完毕，然后中途切入子图内部递归跑完子图的三步符号表决议算法得到 $\Sigma_M$，再从 $\Sigma_M$ 中提取 `v_1, v_2` 的类型反哺回父图 $\Sigma_G$。
* **语法规范阶段无法统一处理 `dom(node.output)`**：当 `InlineGraphNode` 使用列表简写 `output: [v_1, v_2]` 时，在阶段 01 尚未构建子图符号表，导致 `BranchSpec` 消糖与 `ForEachSpec.SingleOut` 被迫为列表简写额外增加特判。

---

### 痛点 7.3：隐式局部变量创建与 `builtin:assign` / `builtin:object_merge` 正反向双模推导引发工作队列死锁

现行规范允许 `builtin:*` 算子与 `for_each`（`filter` / `collect`）直接向从未在图中显式声明过类型的变量写入，并由阶段 2 的工作队列 $Q$ 链式推导其类型。

为此，[`05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md) 中的 `builtin:assign`（当 `source` 为常量字面量时）与 `builtin:object_merge`（当未声明 `base` 时）被迫设计为**“正反向双模推导”**：
* 若目标变量 $v_{\text{out}} \in \text{dom}(\Sigma_G)$，执行**反向实例校验**（$L \models \Sigma_G(v_{\text{out}})$）；
* 若目标变量 $v_{\text{out}} \notin \text{dom}(\Sigma_G)$，执行**正向类型合成**（推导为无 `enum` 标量或全必填闭合 `TypedDict`）。

为了防止某个分支上的 `builtin:assign`（如 `v_out = "a"`）抢在另一个产出带 `enum` 类型的节点之前把 `v_out` 窄化注册为无 `enum` 的普通 `string`，[`03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md#L50-L51) 规定了如下就绪等待条件：
> “若 $M$ 为绑定常量字面量的 `builtin:assign` 或未声明 `base` 的 `builtin:object_merge`，且其输出变量 $v_{\text{out}} \notin \text{dom}(\Sigma_G)$ **同时也是工作队列 $Q$ 中其他待推导项的写入目标**……则需等待其他节点将 $v_{\text{out}}$ 推入 $\text{dom}(\Sigma_G)$ 后就绪。”

**致命死锁反例**：
当图中两条并列控制分支分别通过两个 `builtin:assign` 节点 $M_1, M_2 \in Q$ 向同一个未显式声明类型的局部变量 `label` 写入常量字面量时：
```yaml
nodes:
  branch_a_assign:
    use: builtin:assign
    with:
      input: { source: { value: "normal" } }
      output: { value: label }
  branch_b_assign:
    use: builtin:assign
    with:
      input: { source: { value: "fallback" } }
      output: { value: label }
```
此时 `label` 既不在 `root.output` 中，也未在 `variables` 中显式声明 `TypeSpec`（$\text{label} \notin \text{dom}(\Sigma_G)$）：
* 扫描 $M_1$（`branch_a_assign`）：发现 `label` 同时也是 $Q$ 中 $M_2$ 的写入目标，按规则挂起等待 $M_2$ 先将 `label` 推入 $\text{dom}(\Sigma_G)$；
* 扫描 $M_2$（`branch_b_assign`）：发现 `label` 同时也是 $Q$ 中 $M_1$ 的写入目标，按规则挂起等待 $M_1$ 先将 `label` 推入 $\text{dom}(\Sigma_G)$；
* **$M_1$ 与 $M_2$ 互锁，$Q$ 无法推进，触发阶段 2 第 4 步“循环类型推导依赖”编译报错！**

同理，若两条分支均使用无 `base` 的 `builtin:object_merge` 构造同一个局部结构体变量，也会立即触发同样的互相等待死锁。

---

### 痛点 7.4：`use: workflow:<path>` 的隐式全量同名映射引发“脆弱被调用方问题”与跨文件耦合

在现行 [`02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md#L48-L88) 中，跨文件调用节点 `CallNode`（`use: workflow:<path>`）支持省略 `with.input` 与 `with.output` 的条目，由编译器自动将 `target.input` 与 `target.output` 中未列出的全部字段按同名绑定到调用方栈帧，并要求调用方通过 `unused_field: null` 显式丢弃不需要的输出字段。

该设计存在三大工程缺陷：
1. **脆弱被调用方问题（Fragile Callee Problem）**：
   * 当被调用的子工作流演进升级、在顶层 `output` 中新增了一个辅助输出字段（如 `trace_id` 或 `metrics`）时，所有调用该子工作流的父工作流都会在未修改任何代码的情况下，被隐式将 `trace_id` / `metrics` 注入父图符号表 $\Sigma_{\text{caller}}$！
   * 若父图中恰好已存在同名但不同用途/类型的局部变量，父工作流将直接编译崩溃。
2. **反直觉的 `f: null` 丢弃负担**：
   * 在主流编程语言中，调用返回多字段对象的函数时只解构所需字段（如 `const { summary } = analyze(doc)`），从未要求把不关心的返回值全部显式写成 `const { summary, raw_tokens: null, debug_log: null } = analyze(doc)`。
3. **破坏单文件自包含的数据流分析（Non-Local Dataflow Analysis）**：
   * 在阶段 03 执行必达定值分析（[`03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)）与并发写互斥分析（[`04-concurrent-disjoint-write-analysis.md`](../manual/spec/03-static-flow-analysis/04-concurrent-disjoint-write-analysis.md)）时，编译器仅看当前图 AST 中 `CallNode` 的代码，**根本无法知道该节点读取了哪些父图变量 `Use(N)`、写入了哪些父图变量 `OutVars(N)`**，必须先加载并解析所有外部被调用工作流文件才能计算本图的定值数据流方程。

---

## 二、 核心设计决策：四项减糖与单遍显式类型检查模型

为彻底根除上述复杂度与死锁漏洞，本 RFC 提出移除上述四项语法糖，确立**“显式类型声明、显式边界绑定、单遍模块化检查”**的语言准则。

---

### 决策 1：`graph.variables` 统一为标准 `TypeSpec`（携带可选 `default` 字段）

移除 `ShorthandVarDecl`（无类型字面量简写）。在 `GraphSpec.variables: Map<Identifier, VarDecl>` 中，每个 `<val>` **必须是一个良构的 `TypeSpec` 映射对象，且允许额外包含可选字段 `default: <any_json_literal>`**：

1. **带初值的局部变量声明（包含 `default` 键）**：
   * 编译期立即执行实例兼容性校验：`VarDecl.default` $\models$ `TypeSpec`（允许 `default: null`、空数组 `default: []`、空映射 `default: {}` 或复合结构体字面量，因 `TypeSpec` 已知，无需等待任何后续阶段）；
   * 在阶段 03 必达定值分析中，该变量自动加入起点初始定值集 $Def_{\text{in}}(\text{start})$；
   * 在运行期栈帧创建时，以 `default` 字面量的深拷贝初始化 $\rho_0(v)$。
2. **未初始化的局部变量类型声明（省略 `default` 键）**：
   * 仅在当前图符号表 $\Sigma_G$ 中注册变量类型 $\Sigma_G(v) = \text{TypeSpec}$（等价于 TypeScript / Rust 中的 `let v: T;`）；
   * **不进入**起点初始定值集 $Def_{\text{in}}(\text{start})$（运行期栈帧创建时处于未绑定状态 `Unbound`，必须经由后续节点写入定值后方可被读取，受阶段 03 必达定值分析严格保护）。

> **注意**：由于是通过 AST 映射中“是否存在键 `"default"`”来区分是否带有初值，当变量为可空类型 `type: ["string", "null"]` 时：
> * 显式书写 `default: null` $\implies$ 在 `start` 处已初始化为 `null`（$v \in Def_{\text{in}}(\text{start})$）；
> * 省略 `default` 键 $\implies$ 仅声明类型，在 `start` 处未定值（$v \notin Def_{\text{in}}(\text{start})$）。
> 语义 100% 正交且无任何歧义。

---

### 决策 2：`InlineGraphNode.output` 统一要求显式 `Map<Identifier, TypeSpec>`

移除 `InlineGraphNode.output` 的 `List<Identifier>` 列表简写。所有三类声明自身输出契约的节点（`RootNode`、`ActionNode`、`InlineGraphNode`）的 `output` 字段形态完全统一为：
$$\text{output}: \text{Map}\langle\text{Identifier}, \text{TypeSpec}\rangle$$

* 父图构建符号表时，直接读取 `InlineGraphNode.output` 的显式 `TypeSpec` 映射，无需递归进入子图；
* 子图独立校验自身时，直接将其外层 `InlineGraphNode.output` 作为子图的宿主输出契约 `host.output` 并入子图符号表 $\Sigma_{\text{sub}}$，并在子图 `end` / `end.<branch>` 处校验类型与必达定值。

---

### 决策 3：确立“显式符号表原则（Declaration-Before-Use）”，废除三步工作队列不动点推导

规定每张图 $G$ 的符号表 $\Sigma_G$ **完全由显式 `TypeSpec` 声明源在单遍扫描中构建完毕**。任何节点（包括 `builtin:*`、`use: workflow:<path>` 与 `for_each`）均不再向 $\Sigma_G$ 隐式推导注入未声明的新变量类型：

1. **步骤 1：单遍 $O(N)$ 显式符号收集（Single-Pass Explicit Symbol Collection）**：
   直接将以下四类显式类型源并入 $\Sigma_G$（若同一变量名在多处显式声明，按同帧同名同类型不变式校验 $T_1 \equiv T_2$）：
   * **宿主输入契约 $\Sigma_{\text{host}}(G)$**：
     * 若 $G$ 为根图：$\text{root.input}$；
     * 若 $G$ 为内联子图节点 $N$ 的图：从父图已就绪的 $\Sigma_{\text{outer}}$ 中提取 $N.\text{input}$ 及 $N.\text{for\_each}$ 注入项 $\Sigma_{\text{injected}}(N)$；
   * **宿主输出契约**：$\text{root.output}$ 或内联子图的 $N.\text{output}$；
   * **图级变量声明表 `G.variables`**：所有声明项 $(v, \text{TypeSpec})$（若含 `default` 则同步校验 $\text{default} \models \text{TypeSpec}$）；
   * **图内未挂载 `for_each` 的 `ActionNode.output` 与 `InlineGraphNode.output`**：直接将其显式声明的 $(v, \text{TypeSpec})$ 并入 $\Sigma_G$。
   *(注：若图内某变量仅由 `builtin:*`、`use: workflow:<path>` 或 `for_each.filter / collect` 写入，且不属于 `host.input`、`host.output` 或其他 `run`/`graph` 节点的显式 `output`，则作者必须在 `G.variables` 中声明其 `TypeSpec`。)*
2. **步骤 2：无序独立的节点级静态类型检查（Order-Independent Node Type Checking）**：
   此时 $\Sigma_G$ 已 100% 构建完毕且不可变。静态检查器按任意顺序独立检查图内每个节点 $M$：
   * 所有目标写入变量 $v_{\text{out}}$ 必须已存在于 $\text{dom}(\Sigma_G)$ 中，否则直接报“未声明变量类型（Undeclared Variable Type）”错误；
   * **`builtin:assign`**：
     * 当 `source` 为变量/路径引用 $E$ 时，校验 $\text{TypeOf}(\Sigma_G, E) \equiv \Sigma_G(v_{\text{out}})$；
     * 当 `source` 为常量字面量 $L$ 时，直接校验 $L \models \Sigma_G(v_{\text{out}})$（**不再区分正反向推导，彻底消除工作队列等待与死锁**）；
   * **`builtin:object_merge`**：
     * 无论是否声明 `base`，输出变量 $v_{\text{res}}$ 的目标结构体类型 $T_{\text{target}} = \Sigma_G(v_{\text{res}})$ 均已确定为非空 `TypedDict`；
     * 当未声明 `base` 时，仅需校验输入字段集满足 $\text{set}(T_{\text{target}}.\text{required}) \subseteq \{f_1, \dots, f_m\} \subseteq \text{dom}(T_{\text{target}}.\text{properties})$ 且各字段类型/字面量与 $T_{\text{target}}.\text{properties}[f_i]$ 兼容（**彻底废除情况 B.2 的正向拼凑推导与工作队列等待**）；
   * **其余 `builtin:*` 算子与 `for_each` 通道**：
     * 直接校验算子或 `filter` / `collect` 规则确定的输出类型与 $\Sigma_G(v_{\text{out}})$ 静态等价（$T_{\text{derived}} \equiv \Sigma_G(v_{\text{out}})$）。

---

### 决策 4：`use: workflow:<path>` 取消 `with.input` / `with.output` 隐式同名补全与 `f: null` 语法

将跨文件调用节点 `CallNode`（`use: workflow:<path>`）的数据绑定契约改为与内建算子一致的**显式边界映射**：

1. **`with.input` 显式传参约束**：
   * 被调用工作流顶层声明的每一个输入参数 $p \in \text{dom}(\text{target.input})$，均必须在 `with.input` 中显式写出（若调用方变量与参数同名，显式写作 `p: p`）；
   * 当 $\text{dom}(\text{target.input}) = \emptyset$ 时，允许省略 `with.input`；否则缺失任何目标入参或包含未知入参均在编译期报错。
2. **`with.output` 显式接收与未列出字段默认忽略（废除 `f: null`）**：
   * 当 `CallNode` 未挂载 `for_each` 时：
     * `with.output: Map<Identifier, Identifier>` 中的每个键 $f$ 必须满足 $f \in \text{dom}(\text{target.output})$，每个值 `<local_var>` 必须为合法顶层 `Identifier`（**禁止将值设为 `null`**），且满足 $\Sigma_{\text{caller}}(\text{local\_var}) \equiv \text{target.output}[f]$；
     * 不同键映射的目标变量 `<local_var>` 必须两两互异（单射约束）；
     * **默认忽略未绑定输出**：凡未在 `with.output` 中列出的目标输出字段 $f' \in \text{dom}(\text{target.output}) \setminus \text{dom}(N.\text{with.output})$，在运行期完成被调用方自身的出口契约校验后自动丢弃，不写回调用方栈帧，也不要求在调用方符号表中存在。

---

### 范式迁移对比示例（Before vs. After）

```yaml
# ==========================================
# 重构前（依赖 4 项隐式语法糖）
# ==========================================
graph:
  variables:
    retry_count: 0            # 糖 1：无类型标量简写
    collected_notes: []       # 糖 1：空数组简写（需挂起到阶段 3 逆向校验）

  nodes:
    init_label:
      use: builtin:assign
      with:
        input: { source: { value: "initial" } }
        output: { value: stage_label }  # 糖 3：未声明 stage_label，靠工作队列正向推导

    sub_pipeline:
      input: [raw_doc]
      output: [cleaned_doc]   # 糖 2：内联子图 output 列表简写（父图需等子图推导完）
      graph: ...

    call_analyzer:
      use: workflow:analyzer
      with:
        # 糖 4：省略了同名入参 cleaned_doc，且被迫用 null 显式丢弃不需要的 debug_trace
        output:
          report: final_report
          debug_trace: null

# ==========================================
# 重构后（显式符号表 + 显式边界映射）
# ==========================================
graph:
  variables:
    retry_count:
      type: integer
      default: 0
    collected_notes:
      type: array
      items: { type: string }
      default: []
    stage_label:              # 显式声明未初始化局部变量（等同于 let stage_label: string;）
      type: string
    cleaned_doc:
      type: string

  nodes:
    init_label:
      use: builtin:assign
      with:
        input: { source: { value: "initial" } }
        output: { value: stage_label }  # 直接查 Σ_G(stage_label) 校验实例兼容性，零死锁

    sub_pipeline:
      input: [raw_doc]
      output:                 # 与 ActionNode / RootNode 完全一致的显式 TypeSpec 映射
        cleaned_doc: { type: string }
      graph: ...

    call_analyzer:
      use: workflow:analyzer
      with:
        input:
          cleaned_doc: cleaned_doc      # 显式列出传入参数，单文件即可直接计算 Use(N)
        output:
          report: final_report          # 仅绑定需要的输出，未列出的 debug_trace 自动忽略
```

---

## 三、 `manual/spec/` 形式化规范逐文件修改方案

本节列出 `manual/spec/` 下受四项减糖影响的文件修订内容。

---

### 1. [`index.md`](../manual/spec/index.md)

* 将五阶段流水线总表中 **`02. 静态类型与符号表`** 的职责描述由：
  > 校验 `TypeSpec` 闭合良构性（`TypedDict` 与 `OrderedMap` 互斥）；按三步工作队列不动点决议算法（Worklist Symbol Resolution）构建每张图的符号表 $\Sigma_G$ 与 `for_each` 单次迭代局部符号表 $\Sigma_{\text{iter}}(M)$，强制同帧同名同类型不变式（$T_1 \equiv T_2$），完成静态字段路径投影（含非必填叶子字段可空提升）与 9 个 `builtin:*` 算子的泛型类型推导。
  改为：
  > 校验 `TypeSpec` 闭合良构性（`TypedDict` 与 `OrderedMap` 互斥）；按**单遍显式符号收集算法（Single-Pass Explicit Symbol Resolution）**构建每张图的符号表 $\Sigma_G$ 与 `for_each` 单次迭代局部符号表 $\Sigma_{\text{iter}}(M)$，强制变量先声明后使用（Declaration-Before-Use）与同帧同名同类型不变式（$T_1 \equiv T_2$），完成静态字段路径投影（含非必填叶子字段可空提升）与 9 个 `builtin:*` 算子的静态类型检查。

---

### 2. [`01-grammar-and-ast/01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md)

1. **第二节“图内节点（`InnerNode`）的三态鉴别与字段约束”**：
   * 在表格中，将 `InlineGraphNode` 的允许可选字段 `output: List<Identifier> | Map<Identifier, TypeSpec>` 修改为与 `ActionNode` 完全一致的：
     > `output: Map<Identifier, TypeSpec>`
   * 删除列表下方的“**`InlineGraphNode.output` 列表简写去重约束**”条目。
2. **第四节“控制流图容器（`GraphSpec`）文法与良构性公理”第 4 项 `variables`**：
   * 移除 `ExplicitVarDecl` 与 `ShorthandVarDecl` 的二态消歧规则，替换为单一确定性规范：
     > **`variables?: Map<Identifier, VarDecl>`（可选，默认 `{}`）**：
     > * 每个键 `<v>` 必须为合法 `Identifier`。
     > * 每个值 `<val>`（`VarDecl`）必须是一个映射对象，其除去可选键 `default` 后的其余键值映射必须构成一个良构的 [`TypeSpec`](../02-type-system-and-symbols/01-typespec-well-formedness.md)。
     > * 若 `<val>` 显式包含键 `default`（记为 `v in InitVars(G)`），则表示该变量在图启动时持有初始默认值 `default_literal`；若 `<val>` 未包含键 `default`，则仅声明该图级局部变量的静态类型，图启动时处于未定值状态（`Unbound`）。
3. **第五.1 节“集合遍历修饰符（`ForEachSpec`）”**：
   * $\text{SingleOut}(N)$ 的定义中移除“（若 `InlineGraphNode.output` 为列表简写，则为其关联的子图符号表类型映射）”括号附注。

---

### 3. [`01-grammar-and-ast/02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md)

1. **第二节表格**：
   * 将 `output / with.output.<field>` 行的允许表达式形态由“仅限顶层 `Identifier` ($k = 0$) 或 `null`（仅限 `with.output`）”修改为：
     > 仅限顶层 `Identifier` ($k = 0$)
2. **第三.1 节（原“`with.input` 默认同名补全与必填显式约束”）**：
   * 标题修改为：`### 1. with.input 必填参数显式声明约束`；
   * 移除 `use: workflow:<path>` 的隐式同名自动补全规则，统一规定：
     > 无论 `CallNode` 是调用外部子工作流（`use: workflow:<path>`）还是调用内建算子（`use: builtin:<op>`），**均不支持 `with.input` 的隐式同名省略**：
     > * 对 `use: workflow:<path>`：必须满足 $\text{dom}(N.\text{with.input}) = \text{dom}(\text{target.input})$（当 $\text{target.input}$ 非空时，每个目标参数 $p \in \text{dom}(\text{target.input})$ 均须在 `with.input` 中显式写出；若与外层变量同名，写作 `p: p`）；缺失任何目标参数或包含不属于 $\text{dom}(\text{target.input})$ 的键名，编译期报错。
     > * 对 `use: builtin:<op>`：算子签名要求的全部必填 `with.input` 参数与必填 `with.output` 参数必须在 `with` 中显式写出，缺失任何必填项或包含未知参数名均在编译期报错。
3. **第三.3 节（原“`with.output.<target_out_field>` 的映射、显式丢弃（`null`）与单射规则”）**：
   * 标题修改为：`### 3. with.output.<target_out_field> 的显式映射、未绑定默认忽略与单射规则`；
   * 规则修订为：
     * **键与值合法性**：`with.output`（可选，默认 `{}`）中的每个键 $f$ 必须满足 $f \in \text{dom}(\text{target.output})$，每个值 `<local_var>` 必须为合法顶层 `Identifier`（**禁止将值设为 `null`**）。
     * **显式映射与未列出字段默认忽略**：
       1. 对每个显式列出的条目 `f: <local_var>`，将目标输出字段 $f$ 绑定至调用方图的顶层变量 `<local_var>`；
       2. 对未在 `with.output` 中列出的任何目标输出字段 $f' \in \text{dom}(\text{target.output}) \setminus \text{dom}(N.\text{with.output})$，默认表示**忽略（Ignore / Discard）**：既不要求在调用方符号表 $\Sigma_{\text{caller}}$ 中存在，也不在运行期写回调用方栈帧（被调用方内部出口仍照常执行 `target.output` 的契约校验）。
     * **单射约束（Injective Mapping）**：
       $$\forall f_1, f_2 \in \text{dom}(N.\text{with.output}), \quad f_1 \neq f_2 \implies N.\text{with.output}[f_1] \neq N.\text{with.output}[f_2]$$
       即两个不同的目标输出字段禁止映射到同一个调用方图变量 `<local_var>`。

---

### 4. [`02-type-system-and-symbols/03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md)

将原“三步工作队列不动点决议算法（Worklist Symbol Resolution）”全面重写为**“单遍显式符号表构建与模块化类型检查算法（Single-Pass Explicit Symbol Resolution & Modular Type Checking）”**：

1. **阶段 1：单遍 $O(N)$ 显式符号表构建（Explicit Symbol Table Construction）**：
   对于任意图 $G$，其符号表 $\Sigma_G : \text{Identifier} \rightharpoonup \text{TypeSpec}$ 仅通过单遍收集以下四类显式 `TypeSpec` 声明源构建（每次并入 $(v, T)$ 时，若 $v \in \text{dom}(\Sigma_G)$ 则校验 $\Sigma_G(v) \equiv T$）：
   * **宿主输入符号表 $\Sigma_{\text{host}}(G)$**：
     * 若 $G$ 为根图：$\Sigma_{\text{host}}(G) = \text{root.input}$；
     * 若 $G$ 为内联子图节点 $N$ 的图：从父图 $\Sigma_{\text{outer}}$ 提取 $N.\text{input}$（及 `for_each.state`，若未应用 `RFC-0006`）并合入 $\Sigma_{\text{injected}}(N)$；
   * **宿主输出契约 `host.output`**：根图的 `root.output` 或内联子图的 $N.\text{output}$；
   * **图级变量表 `G.variables`**：对每个声明项 $v \mapsto (\text{TypeSpec}_v, \text{default}_v?)$，将 $(v, \text{TypeSpec}_v)$ 并入 $\Sigma_G$；若声明了 `default` 键，立即校验 $\text{default}_v \models \text{TypeSpec}_v$；
   * **图内未挂载 `for_each` 的 `ActionNode.output` 与 `InlineGraphNode.output`**：对图内每个未挂载 `for_each` 的 `ActionNode` 或 `InlineGraphNode` $M$，将其显式声明的 $M.\text{output}$ 中的所有 $(v, T)$ 并入 $\Sigma_G$。
2. **阶段 2：无序独立的节点级静态类型检查（Order-Independent Node Type Checking）**：
   在 $\Sigma_G$ 构建完毕后，对图 $G$ 内每个节点 $M \in \text{dom}(G.\text{nodes})$ 独立执行静态类型校验：
   * **未挂载 `for_each` 的 `ActionNode`（`run`）**：
     * 校验 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_G)$；
     * 校验 `run` 字符串内所有 `variables.<E>` 引用在 $\Sigma_G|_{M.\text{input}}$ 下通过 `TypeOf` 检查。
   * **未挂载 `for_each` 的 `InlineGraphNode`（内联 `graph`）**：
     * 校验 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_G)$；
     * 以 $\Sigma_{\text{host}}(M) = \Sigma_G|_{M.\text{input}}$ 和 $\text{host.output} = M.\text{output}$ 递归独立校验子图 $M.\text{graph}$。
   * **未挂载 `for_each` 的 `CallNode`（`use: workflow:<path>`）**：
     * 校验每个入参绑定 `p: val`（$p \in \text{dom}(\text{target.input})$）：若 `val` 为引用表达式 $E$，须满足 $\text{TypeOf}(\Sigma_G, E) \equiv \text{target.input}[p]$；若 `val` 为常量字面量 $L$，须满足 $L \models \text{target.input}[p]$；
     * 校验每个显式出参绑定 `f: local_var`（$f \in \text{dom}(M.\text{with.output})$）：须满足 $\text{local\_var} \in \text{dom}(\Sigma_G)$ 且 $\Sigma_G(\text{local\_var}) \equiv \text{target.output}[f]$。
   * **内建算子节点（`use: builtin:<op>`）**：
     * 按 [`05-builtin-generic-inference.md`](./05-builtin-generic-inference.md) 校验所有入参引用/字面量与出参目标变量在 $\Sigma_G$ 中的已声明类型完全匹配（其中出参变量 $v_{\text{out}}$ 必须满足 $v_{\text{out}} \in \text{dom}(\Sigma_G)$）。
   * **挂载了 `for_each` 的节点 $M$**：
     * 校验源集合 $T_{\text{src}} = \text{TypeOf}(\Sigma_G, M.\text{for\_each.source})$ 为非空 `Array<T>` 或 `OrderedMap<string, TV>`，构造 $\Sigma_{\text{injected}}(M)$；
     * 在单次迭代局部符号表 $\Sigma_{\text{iter}}(M) = \Sigma_G \uplus \Sigma_{\text{injected}}(M)$（对 `ActionNode` / `CallNode`）或 $\Sigma_{\text{host}}(M)$（对 `InlineGraphNode`）下校验节点内部入参与子图；
     * 校验 `for_each.filter` 的每个目标变量 $v_{\text{flt}}$ 满足 $v_{\text{flt}} \in \text{dom}(\Sigma_G) \land \Sigma_G(v_{\text{flt}}) \equiv T_{\text{src}}$；
     * 校验 `for_each.collect` 的每个目标变量 $v_{\text{col}}$ 满足 $v_{\text{col}} \in \text{dom}(\Sigma_G)$ 且与提升后的集合类型静态等价。

---

### 5. [`02-type-system-and-symbols/05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md)

将所有算子的“出参推导并注册 $\Sigma(v_{\text{out}}) = T$”统一简化为**“校验 $v_{\text{out}} \in \text{dom}(\Sigma)$ 且 $\Sigma(v_{\text{out}}) \equiv T$”**，并消除 `builtin:assign` 与 `builtin:object_merge` 的正反向双模分支：

1. **`builtin:assign`（第 3 节）**：
   * 要求出参 `value` 绑定的顶层变量 $v_{\text{out}} \in \text{dom}(\Sigma)$，记目标类型为 $T_{\text{target}} = \Sigma(v_{\text{out}})$：
     * 当 `with.input.source` 为变量或静态字段路径引用 $E$ 时：校验 $\text{TypeOf}(\Sigma, E) \equiv T_{\text{target}}$；
     * 当 `with.input.source` 为常量字面量 $L$ 时：直接校验实例兼容性 $L \models T_{\text{target}}$。
     *(彻底移除原“当 $v_{\text{out}} \notin \text{dom}(\Sigma)$ 时推导为基础标量类型”的分支。)*
2. **`builtin:object_merge`（第 9 节）**：
   * 要求出参 `result` 绑定的顶层变量 $v_{\text{res}} \in \text{dom}(\Sigma)$，且目标类型 $T_{\text{target}} = \Sigma(v_{\text{res}})$ 必须为非空强类型结构体（`TypedDict`）；
   * **情况 A（声明了 `base`）**：校验 $\text{TypeOf}(\Sigma, \text{with.input.base}) \equiv T_{\text{target}}$，且每个覆盖字段 $f_i \in \text{dom}(T_{\text{target}}.\text{properties})$ 的引用或字面量与目标属性类型兼容；
   * **情况 B（未声明 `base`）**：直接校验 $\text{set}(T_{\text{target}}.\text{required}) \subseteq \{f_1, \dots, f_m\} \subseteq \text{dom}(T_{\text{target}}.\text{properties})$，且每个字段 $f_i$ 的引用或字面量与 $T_{\text{target}}.\text{properties}[f_i]$ 兼容。
     *(彻底移除原情况 B.2“当 $v_{\text{res}} \notin \text{dom}(\Sigma)$ 时正向推导构造闭合 `TypedDict`”的分支。)*

---

### 6. [`03-static-flow-analysis/02-scoping-and-collision-rules.md`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md) 与 [`03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)

1. **[`02-scoping-and-collision-rules.md`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md)**：
   * 第一节“宿主上下文输入与 `graph.variables` 互斥公理”保持：
     $$\text{HostIn}(G) \cap \text{dom}(G.\text{variables}) = \emptyset$$
     （凡属于 $\text{HostIn}(G)$ 的变量已在宿主输入契约中拥有明确类型与初始运行期值，禁止在 $G.\text{variables}$ 中重复声明类型或初值。）
   * 第三节表格中，`CallNode` 行的 `M.with.input` 说明移除“（含默认同名补全项）”。
2. **[`03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)**：
   * **第一节“起点出边初始定值集 $Def_{\text{in}}(\text{start})$”**：
     定义 $G.\text{variables}$ 中显式包含 `default` 初值的变量子集为：
     $$\text{InitVars}(G) = \{ v \in \text{dom}(G.\text{variables}) \mid \texttt{default} \in \text{dom}(G.\text{variables}[v]) \}$$
     将起点初始定值集公式更新为：
     $$Def_{\text{in}}(\text{start}) = \text{HostIn}(G) \cup \text{InitVars}(G)$$
     *(注：在 $G.\text{variables}$ 中仅声明了 `TypeSpec` 但未声明 `default` 初值的局部变量不属于 $\text{InitVars}(G)$，在 `start` 处处于未定值状态，必须由后续节点定值后方可读取。)*
   * **第二节“边级定值传递方程”与第四节“节点入口读取集断言”**：
     * 由于 `CallNode` 的 `with.input` 与 `with.output` 均已显式列出，计算 $Use(N)$ 与 $OutVars(N)$ 直接读取当前节点 AST 的 `N.with.input` 与 `values(N.with.output)`，**不再需要跨文件查询被调用工作流签名**即可完成本图的全路径必达定值分析与并发写互斥分析。

---

### 7. [`04-runtime-execution-model/01-frame-memory-model.md`](../manual/spec/04-runtime-execution-model/01-frame-memory-model.md)

1. **第一.1 节 `FunctionFrame` 与第一.2 节 `BlockFrame`**：
   * 初始内存绑定 $\rho_0$ 中的 $\rho_{\text{graph\_defaults}}$ / $\rho_{\text{subgraph\_defaults}}$ 明确定义为仅提取 $G.\text{variables}$ 中声明了 `default` 键的变量子集 $\text{InitVars}(G)$ 的初始值深拷贝；未声明 `default` 键的局部变量在栈帧创建时保持未绑定（`Unbound`）。
   * `FunctionFrame` 正常提交说明中，将“忽略映射为 `null` 的显式丢弃字段”更新为“仅提取 `with.output` 中显式映射的目标字段写回父栈帧，未在 `with.output` 中列出的其余输出字段自动丢弃”。

---

## 四、 `manual/docs/`（指南、参考与配方）同步修订清单

在实施本 RFC 时，需同步更新 `manual/docs/` 中的以下文档示例与说明：

1. **[`guide/01-foundations/01-basics.md`](../manual/docs/guide/01-foundations/01-basics.md)** 与 **[`guide/04-state-and-resilience/01-frames-and-scoping.md`](../manual/docs/guide/04-state-and-resilience/01-frames-and-scoping.md)**：
   * 更新 `graph.variables` 的语法说明：展示带 `default` 的初始化变量声明与不带 `default` 的未初始化局部变量声明（`let v: T`），移除裸标量/裸数组字面量简写示例。
2. **[`guide/01-foundations/02-use-subworkflows.md`](../manual/docs/guide/01-foundations/02-use-subworkflows.md)**：
   * 更新 `use: workflow:<path>` 的 `with.input` 与 `with.output` 规则：明确入参显式绑定、出参按需映射且未列出的输出字段默认自动忽略，删除 `field: null` 显式丢弃小节与示例。
3. **[`guide/01-foundations/03-inline-graphs.md`](../manual/docs/guide/01-foundations/03-inline-graphs.md)**：
   * 将内联 `graph` 节点的 `output: [v1, v2]` 列表简写示例全部更新为显式 `output: { v1: <TypeSpec>, v2: <TypeSpec> }`。
4. **[`reference/01-node-and-graph-fields.md`](../manual/docs/reference/01-node-and-graph-fields.md)**、**[`reference/builtins/assign.md`](../manual/docs/reference/builtins/assign.md)** 与 **[`reference/builtins/object-merge.md`](../manual/docs/reference/builtins/object-merge.md)**：
   * 更新字段参考表中的 `InlineGraphNode.output`、`GraphSpec.variables`、`with.input`、`with.output` 约束描述；
   * 在 `builtin:assign` 与 `builtin:object_merge` 参考页中说明目标输出变量须在图符号表中显式声明类型。
5. **[`cookbook/`](../manual/docs/cookbook/index.md) 与 [`guide/`](../manual/docs/guide/index.md) 全量 YAML 示例巡检**：
   * 将所有示例中的 `variables` 标量简写（如 `attempt: 0`）更新为 `{ type: integer, default: 0 }`；
   * 为仅由 `builtin:*` / `for_each` 写入的局部中间变量在 `variables` 中补齐显式 `TypeSpec` 声明；
   * 将内联子图的 `output: [...]` 列表简写更新为显式 `TypeSpec` 映射，并为 `use: workflow:<path>` 节点补齐显式 `with.input` / `with.output` 映射（移除 `: null`）。
