# 03. 图级静态符号表三步工作队列不动点决议算法（Worklist Symbol Resolution）

运行时在编译期按**从外层图到内层子图（Top-Down）**与**三步工作队列不动点迭代（Worklist Fixed-Point Iteration）**相结合的顺序，为每一张 `graph` $G$ 构建图级静态符号表 $\Sigma_G : \text{Identifier} \rightharpoonup \text{TypeSpec}$，并为挂载了 `for_each` 的节点 $M$ 构建单次迭代局部符号表 $\Sigma_{\text{iter}}(M)$。

> **同帧同名同类型不变式（Same-Frame Type Invariance）与符号表并入规则**：
> 向符号表 $\Sigma_G$ 并入类型绑定 $(v, T)$ 时：若 $v \notin \text{dom}(\Sigma_G)$，则注册 $\Sigma_G(v) \leftarrow T$；若 $v \in \text{dom}(\Sigma_G)$，则必须满足静态类型等价 $\Sigma_G(v) \equiv T$（判定规则见 [`02-type-equivalence-and-compatibility.md`](./02-type-equivalence-and-compatibility.md)），否则静态检查器在编译期报错。

---

## 一、 图级符号表 $\Sigma_G$ 的三步工作队列不动点决议算法

对于当前图 $G$，符号表 $\Sigma_G$ 按以下三个阶段依次构建，彻底消除显式类型声明、`G.variables` 孤立标量简写、跨节点链式泛型推导（`builtin:*`、`for_each`）与内联子图列表简写之间的顺序死锁：

### 1. 阶段 1：无依赖显式类型收集与 `G.variables` 孤立标量预注册

1. **收集所有无需依赖其他节点推导结果的显式 `(var_name, TypeSpec)` 对**：
   * **宿主上下文输入与输出契约**：
     * 若 $G$ 为根节点的图：将 $\text{root.input}$ 与 $\text{root.output}$ 中的所有 $(v, T)$ 并入 $\Sigma_G$；
     * 若 $G$ 为内联子图节点 $N$ 的图（父图在触发 $N$ 的子图决议前已在阶段 2 确定 $N$ 依赖的全部外层输入类型）：将子图初始宿主输入符号表 $\Sigma_{\text{host}}(N)$（定义见第二节）中的所有 $(v, T)$ 并入 $\Sigma_G$；若 $N.\text{output}$ 采用完整映射形式 $\{ \langle v \rangle: \langle \text{TypeSpec} \rangle \}$，将其中的所有 $(v, T)$ 并入 $\Sigma_G$。
   * **`G.variables` 中显式带 `TypeSpec` 的声明项**：
     * 对 `G.variables` 中形如 `<v>: { <TypeSpec>, default: <literal> }` 的项，校验 $\text{literal} \models \text{TypeSpec}$ 后，将 $(v, \text{TypeSpec})$ 并入 $\Sigma_G$。
   * **图内未挂载 `for_each` 节点的显式输出**：
     * 若子节点 $M$ 为未挂载 `for_each` 的 `ActionNode`（`run`）：将 $M.\text{output}$ 中的所有 $(v, T)$ 并入 $\Sigma_G$；
     * 若子节点 $M$ 为未挂载 `for_each` 的 `InlineGraphNode`（`graph`）且 $M.\text{output}$ 为完整映射形式 `{ <v>: <TypeSpec> }`：将其中的所有 $(v, T)$ 并入 $\Sigma_G$；
     * 若子节点 $M$ 为未挂载 `for_each` 的跨文件 `CallNode`（`use: workflow:<path>`）：直接读取被调用工作流顶层显式声明的 `target.output`，将其经 $M.\text{with.output}$ 映射后的非空目标 `(local_var, T)` 并入 $\Sigma_G$（剔除显式映射为 `null` 丢弃的字段）。
2. **`G.variables` 孤立标量字面量预注册（Isolated Scalar Shorthand Pre-Registration）**：
   * 定义图 $G$ 内所有子节点的**写入目标变量集合 $\text{WrittenVars}(G)$** 为图内所有子节点的 `output`、`with.output`（非 `null` 目标）、`for_each.filter` 目标变量及 `for_each.collect` 目标变量的并集。
   * 扫描 `G.variables` 中使用字面量简写 `<v>: <literal>` 的每一项：若变量名 $v$ **未出现在当前图内任何节点的写入目标位置**（$v \notin \text{WrittenVars}(G)$）且不属于目前已收集的显式类型集（$v \notin \text{dom}(\Sigma_G)$），则立即校验 `<literal>` 为非空基础标量字面量（`string` / `integer` / `float` / `boolean`），将其推导为对应的无 `enum` 非空基础标量 `TypeSpec` 并并入 $\Sigma_G$（若 `<literal>` 为 `null`、空数组 `[]` 或对象 `{}`，则编译期报错）。

---

### 2. 阶段 2：工作队列迭代推导（处理 `builtin:*`、`for_each` 与内联子图列表简写）

将图 $G$ 内所有待推导项——包括 `use: builtin:*` 节点、挂载了 `for_each` 的节点，以及未挂载 `for_each` 的 `InlineGraphNode`——放入工作队列 $Q$。循环扫描 $Q$ 直至 $Q = \emptyset$：

1. **内建泛型算子节点 $M$（`use: builtin:<op>`）**：
   * **就绪条件**：
     1. $M.\text{with.input}$ 中所有变量或字段路径引用 $E$ 的根变量 $\text{RootVar}(E)$ 均已存在于 $\text{dom}(\Sigma_G)$ 中；
     2. 若 $M$ 为绑定常量字面量的 `builtin:assign` 或未声明 `base` 的 `builtin:object_merge`，且其输出变量 $v_{\text{out}} \notin \text{dom}(\Sigma_G)$ 同时也是工作队列 $Q$ 中其他待推导项的写入目标（或字面量中包含需依赖目标类型 $\Sigma_G(v_{\text{out}})$ 校验的非基础标量字面量），则需等待其他节点将 $v_{\text{out}}$ 推入 $\text{dom}(\Sigma_G)$ 后就绪。
   * **推导与推进**：按 [`05-builtin-generic-inference.md`](./05-builtin-generic-inference.md) 校验入参并推导输出绑定 $(v_{\text{out}}, T_{\text{out}})$（覆盖 `unwrap`、`switch`、`assign`、`array_shift`、`array_append`、`map_set`、`map_get`、`map_delete`、`object_merge` 共 9 个内建算子），并入 $\Sigma_G$（若已存在则校验静态等价 $T_1 \equiv T_2$），将 $M$ 移出 $Q$。
2. **未挂载 `for_each` 的内联子图节点 $M$（`InlineGraphNode`）**：
   * **就绪条件**：$\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_G)$。
   * **推导与推进**：以初始宿主输入符号表 $\Sigma_{\text{host}}(M) = \Sigma_G|_{M.\text{input}}$ 递归执行子图 $M.\text{graph}$ 的完整三步符号表决议算法得到 $\Sigma_M$：
     * 若 $M.\text{output}$ 为列表简写 $[v_1, \dots, v_m]$，从 $\Sigma_M$ 提取每个 $(v_i, \Sigma_M(v_i))$ 并入 $\Sigma_G$（若 $v_i \notin \text{dom}(\Sigma_M)$ 则报错）；
     * 若 $M.\text{output}$ 为完整映射，校验每个字段类型与 $\Sigma_M(v_i)$ 静态等价；
     * 将 $M$ 移出 $Q$。
3. **挂载了 `for_each` 的子节点 $M$**：
   * **就绪条件**：
     $$\text{RootVar}(M.\text{for\_each.source}) \in \text{dom}(\Sigma_G) \;\land\; \text{set}(M.\text{for\_each.state}) \subseteq \text{dom}(\Sigma_G)$$
     且当 $M$ 为 `InlineGraphNode` 时，还须满足 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_G)$。
   * **推导与推进**：
     1. 计算源集合类型 $T_{\text{src}} = \text{TypeOf}(\Sigma_G, M.\text{for\_each.source})$，构造单次迭代注入符号表 $\Sigma_{\text{injected}}(M)$（见第二节）；
     2. 确立节点 $M$ 的单次迭代输出字段契约表 $\text{SingleOut}(M)$：
        * 若 $M$ 为 `ActionNode`：$\text{SingleOut}(M) = M.\text{output}$；
        * 若 $M$ 为 `CallNode`（`use: workflow:<path>`）：$\text{SingleOut}(M) = \text{target.output}$；
        * 若 $M$ 为 `InlineGraphNode`：以 $\Sigma_{\text{host}}(M) = \Sigma_G|_{M.\text{input} \cup M.\text{for\_each.state}} \uplus \Sigma_{\text{injected}}(M)$ 递归运行子图符号表决议算法得到 $\Sigma_M$，并提取（或校验）$M.\text{output}$ 各字段在 $\Sigma_M$ 中的类型作为 $\text{SingleOut}(M)$。
     3. 单次迭代局部产出仅用于内部推导，不加入外层图符号表 $\Sigma_G$；仅将 $M.\text{for\_each}$ 的 `filter`、`collect`、`state` 三通道外层目标变量及提升后的集合类型并入 $\Sigma_G$：
        * **`filter` 目标变量**：类型等于 $T_{\text{src}}$；
        * **`collect` 目标变量（`flatten: false`）**：设单次字段类型为 $U = \text{SingleOut}(M)[f]$，当 $T_{\text{src}}$ 为 `Array<T>` 时提升为 `Array<U>`，当 $T_{\text{src}}$ 为 `OrderedMap<string, TV>` 时提升为 `OrderedMap<string, U>`；
        * **`collect` 目标变量（`flatten: true`）**：要求 $T_{\text{src}}$ 为 `Array<T>` 且单次字段类型为非空 `Array<W>`，提升后为一维 `Array<W>`；
        * **`state` 目标变量**：对每个 $s \in M.\text{for\_each.state}$，校验其在 $\Sigma_G$ 中的类型与单次同名产出字段类型静态等价（$\Sigma_G(s) \equiv \text{SingleOut}(M)[s]$）。
     4. 将 $M$ 移出 $Q$。
4. **不动点收敛与循环依赖判定**：
   * 若在某一轮迭代中 $Q \neq \emptyset$ 但没有任何项可推进，则说明图中存在循环类型推导依赖或引用了未声明变量，静态检查器在编译期报错。

---

### 3. 阶段 3：`G.variables` 剩余字面量简写兼容性校验与兜底推导

扫描 `G.variables` 中尚未在阶段 1 预注册的其余字面量简写项 `<v>: <literal>`：

1. **若 $v \in \text{dom}(\Sigma_G)$（已在阶段 1 或阶段 2 获得目标类型 $T = \Sigma_G(v)$）**：
   * 执行实例兼容性校验 $\text{literal} \models T$；若不兼容则编译期报错。
2. **若 $v \notin \text{dom}(\Sigma_G)$**：
   * `<literal>` 必须为非空基础标量字面量（`string` / `integer` / `float` / `boolean`），自动将其推导为对应的无 `enum` 非空基础标量 `TypeSpec` 并注册至 $\Sigma_G(v)$；
   * 若 `<literal>` 为 `null`、空数组 `[]` 或对象 `{}` 且 $v \notin \text{dom}(\Sigma_G)$，编译期报错（必须改用显式带类型声明 `{ <TypeSpec>, default: <literal> }`）。

---

## 二、 `for_each` 单次迭代局部符号表 $\Sigma_{\text{iter}}(M)$ 与节点内部解析规则

当外层图 $G_{\text{outer}}$（符号表为 $\Sigma_{\text{outer}}$）中的节点 $M$ 挂载了 `for_each` 时，编译器按以下规则构造单次迭代注入符号表与节点内部解析符号表：

### 1. 单次迭代注入符号表 $\Sigma_{\text{injected}}(M)$

设源集合静态类型为 $T_{\text{src}} = \text{TypeOf}(\Sigma_{\text{outer}}, M.\text{for\_each.source})$：

* **当 $T_{\text{src}}$ 为强类型列表 `{ type: array, items: T_elem }` 时**：
  $$\Sigma_{\text{injected}}(M) = \{ M.\text{for\_each.item} \mapsto T_{\text{elem}} \} \cup \{ M.\text{for\_each.index} \mapsto \{\texttt{type}: \texttt{integer}\} \text{ (若声明)} \}$$
* **当 $T_{\text{src}}$ 为强类型有序映射 `{ type: object, additionalProperties: T_val }` 时**：
  $$\Sigma_{\text{injected}}(M) = \{ M.\text{for\_each.item} \mapsto T_{\text{val}} \} \cup \{ M.\text{for\_each.key} \mapsto \{\texttt{type}: \texttt{string}\} \text{ (若声明)} \} \cup \{ M.\text{for\_each.index} \mapsto \{\texttt{type}: \texttt{integer}\} \text{ (若声明)} \}$$

### 2. 三类节点的内部解析符号表与 `input` 捕获校验规则

1. **当 $M$ 为挂载了 `for_each` 的 `ActionNode`（`run`）或 `CallNode`（`use: workflow:<path>`）时**：
   定义节点 $M$ 的**单次迭代局部符号表 $\Sigma_{\text{iter}}(M)$** 为：
   $$\Sigma_{\text{iter}}(M) = \Sigma_{\text{outer}} \uplus \Sigma_{\text{injected}}(M)$$
   *(注：$\text{dom}(\Sigma_{\text{outer}}) \cap \text{dom}(\Sigma_{\text{injected}}(M)) = \emptyset$ 由 [`02-scoping-and-collision-rules.md`](../03-static-flow-analysis/02-scoping-and-collision-rules.md) 的注入名无冲突公理保证。)*
   * **对 `ActionNode`（`run`）**：
     * 要求 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_{\text{iter}}(M))$（根据 [`01-run-executor-contract.md`](../05-executor-abi/01-run-executor-contract.md)，`ActionNode` 的 `input` 必须显式列出 `run` 所需读取的 `item` / `key` / `index`、`state` 及外层变量，否则不会进入执行器的 `input_snapshot`）；
     * `run` 内部所有 `variables.<E>` 引用必须在受限符号表 $\Sigma_{\text{iter}}(M)|_{M.\text{input}}$ 下通过 [`TypeOf`](./04-static-field-projection-typing.md) 校验。
   * **对 `CallNode`（`use: workflow:<path>`）**：
     * 要求 $M.\text{with.input}$（含默认同名补全项）中的所有变量或路径引用 $E$ 在 $\Sigma_{\text{iter}}(M)$ 下通过 $\text{TypeOf}(\Sigma_{\text{iter}}(M), E)$ 校验，且与被调用工作流顶层 $\text{target.input}$ 对应参数类型静态等价。
2. **当 $M$ 为挂载了 `for_each` 的 `InlineGraphNode`（内联 `graph`）时**：
   * 子图初始宿主输入符号表为：
     $$\Sigma_{\text{host}}(M) = \Sigma_{\text{outer}}|_{M.\text{input} \cup M.\text{for\_each.state}} \uplus \Sigma_{\text{injected}}(M)$$
   * 要求 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_{\text{outer}})$，且由于 $\Sigma_{\text{injected}}(M)$ 与 $M.\text{for\_each.state}$ 已自动注入子图初始上下文，禁止在 $M.\text{input}$ 中重复列出这些变量：
     $$\text{set}(M.\text{input}) \cap (\text{dom}(\Sigma_{\text{injected}}(M)) \cup \text{set}(M.\text{for\_each.state})) = \emptyset$$
