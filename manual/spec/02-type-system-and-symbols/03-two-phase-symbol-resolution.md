# 03. 单遍显式符号表构建与模块化类型检查算法（Single-Pass Explicit Symbol Resolution & Modular Type Checking）

运行时在编译期按**阶段 1：单遍 $O(N)$ 显式符号表构建（双向等价 $\equiv$）**与**阶段 2：无序独立的节点级静态类型检查（单向子类型 $<:$）**两步，为每一张 `graph` $G$ 构建图级静态符号表 $\Sigma_G : \text{RestrictedIdentifier} \rightharpoonup \text{TypeSpec}$，并对图内各节点（含挂载了 `for_each` 的节点 $M$）独立执行静态类型校验。

---

## 一、 辅助算子：一阶集合类型提升 $\text{Lift}$ 与 `for_each` 注入符号表 $\Sigma_{\text{injected}}$

### 1. 一阶同构集合类型提升算子 $\text{Lift}(T_{\text{src}}, U)$

对于非空源集合类型 $T_{\text{src}}$ 与单次迭代产出字段类型 $U$，定义一阶同构集合类型提升算子 $\text{Lift}(T_{\text{src}}, U)$：
$$\text{Lift}(T_{\text{src}}, U) = \begin{cases} \{ \texttt{type}: \texttt{array}, \texttt{items}: U \} & \text{若 } \text{BaseType}(T_{\text{src}}) = \texttt{array} \\ \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: U \} & \text{若 } T_{\text{src}} \text{ 为 } \texttt{OrderedMap} \end{cases}$$

### 2. `for_each` 单次迭代注入符号表 $\Sigma_{\text{injected}}(M)$

当图 $G$（符号表为 $\Sigma_G$）中的节点 $M$ 挂载了 `for_each`，且源集合静态类型为 $T_{\text{src}} = \text{TypeOf}(\Sigma_G, M.\text{for\_each.source})$ 时，定义单次迭代注入符号表 $\Sigma_{\text{injected}}(M)$：

* **当 $T_{\text{src}}$ 为强类型列表 `{ type: array, items: T_elem }` 时**：
  $$\Sigma_{\text{injected}}(M) = \{ M.\text{for\_each.item} \mapsto T_{\text{elem}} \} \cup \{ M.\text{for\_each.index} \mapsto \{\texttt{type}: \texttt{integer}\} \text{ (若声明)} \}$$
* **当 $T_{\text{src}}$ 为强类型有序映射 `{ type: object, additionalProperties: T_val }` 时**：
  $$\Sigma_{\text{injected}}(M) = \{ M.\text{for\_each.item} \mapsto T_{\text{val}} \} \cup \{ M.\text{for\_each.key} \mapsto \{\texttt{type}: \texttt{string}\} \text{ (若声明)} \} \cup \{ M.\text{for\_each.index} \mapsto \{\texttt{type}: \texttt{integer}\} \text{ (若声明)} \}$$

> **注入名无冲突保证**：由 [`02-scoping-and-collision-rules.md`](../03-static-flow-analysis/02-scoping-and-collision-rules.md) 的注入名无冲突公理保证 $\text{dom}(\Sigma_G) \cap \text{dom}(\Sigma_{\text{injected}}(M)) = \emptyset$。

---

## 二、 阶段 1：单遍 $O(N)$ 显式符号表构建算法（双向等价 $\equiv$）

每张图 $G$ 的符号表 $\Sigma_G : \text{RestrictedIdentifier} \rightharpoonup \text{TypeSpec}$ 完全由以下四类显式 `TypeSpec` 声明源在单遍扫描中构建完毕；任何节点（包括 `builtin:*`、`use: workflow:<path>` 与 `for_each`）均不向 $\Sigma_G$ 隐式注入未声明的新变量类型。

向符号表 $\Sigma_G$ 并入类型绑定 $(v, T)$ 时：
* 若 $v \notin \text{dom}(\Sigma_G)$，则注册 $\Sigma_G(v) \leftarrow T$；
* 若 $v \in \text{dom}(\Sigma_G)$，则必须满足**双向静态类型等价 $\Sigma_G(v) \equiv T$**（判定规则见 [`02-type-equivalence-and-compatibility.md`](./02-type-equivalence-and-compatibility.md)），确保每个变量在所属栈帧中拥有唯一、与收集顺序无关的权威静态类型。

四类显式 `TypeSpec` 声明源如下：

1. **宿主输入符号表 $\Sigma_{\text{host}}(G)$**：
   * 若 $G$ 为根节点的图：$\Sigma_{\text{host}}(G) = \text{root.input}$；
   * 若 $G$ 为父图（符号表为 $\Sigma_{\text{outer}}$）中内联子图节点 $N$ 的图：
     * 当 $N$ 未挂载 `for_each` 时，$\Sigma_{\text{host}}(G) = \Sigma_{\text{outer}}|_{N.\text{input}}$；
     * 当 $N$ 挂载 `for_each` 时，$\Sigma_{\text{host}}(G) = \Sigma_{\text{outer}}|_{N.\text{input}} \uplus \Sigma_{\text{injected}}(N)$。
2. **宿主输出契约 `host.output`**：
   * 若 $G$ 为根节点的图：将 $\text{root.output}$ 中的所有 $(v, T)$ 并入 $\Sigma_G$；
   * 若 $G$ 为内联子图节点 $N$ 的图：将 $N.\text{output}$ 中的所有 $(v, T)$（当 $N$ 挂载 `for_each` 时，即为未提升前的单次迭代输出映射 $N.\text{output}$）并入 $\Sigma_G$。
3. **图级变量表 `G.variables`**：
   * 对 `G.variables` 中的每个声明项 $v \mapsto (\text{TypeSpec}_v, \text{default}_v?)$，将 $(v, \text{TypeSpec}_v)$ 并入 $\Sigma_G$；
   * 若声明了 `default` 键，立即校验实例兼容性 $\text{default}_v \models \text{TypeSpec}_v$（若不兼容则编译期报错）。
4. **图内未挂载 `for_each` 的 `ActionNode.output` 与 `InlineGraphNode.output`**：
   * 对图 $G$ 内每个未挂载 `for_each` 的 `ActionNode`（`run`）或 `InlineGraphNode`（内联 `graph`）$M$，将其显式声明的 $M.\text{output}$ 中的所有 $(v, T)$ 并入 $\Sigma_G$。

> **注**：若图内某变量仅由 `builtin:*`、`use: workflow:<path>` 或挂载了 `for_each` 的节点写入，且不属于 `host.input`、`host.output` 或图内其他未挂载 `for_each` 的 `run` / `graph` 节点的显式 `output`，则必须在 `G.variables` 中显式声明其 `TypeSpec`（无需初值时省略 `default` 键）。

---

## 三、 阶段 2：无序独立的节点级静态类型检查算法（单向子类型 $<:$）

在阶段 1 构建完毕后，$\Sigma_G$ 已完全确定且不可变。当数据从源变量/表达式流向目标参数或已声明的目标变量时，静态检查器按任意顺序对图 $G$ 内每个节点 $M \in \text{dom}(G.\text{nodes})$ 独立执行**单向静态子类型兼容（$T_{\text{actual}} <: T_{\text{expected}}$）**与引用合法性校验：

1. **未挂载 `for_each` 的 `ActionNode`（`run`）**：
   * 校验 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_G)$；
   * 从 $\Sigma_G$ 中按 $M.\text{input}$ 声明顺序提取受限输入符号表 $\text{input\_schema}(M) = \Sigma_G|_{M.\text{input}}$（对顶层 `RootRunNode`，其 $\text{input\_schema} = \text{root.input}$），随归一化 AST 导出至运行时执行器边界（详见 [`01-run-executor-contract.md`](../05-executor-abi/01-run-executor-contract.md)）。编译器前端不解析 `run` 内部特定执行器的业务实现代码。
2. **未挂载 `for_each` 的 `InlineGraphNode`（内联 `graph`）**：
   * 校验 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_G)$；
   * 以 $\Sigma_{\text{host}}(M.\text{graph}) = \Sigma_G|_{M.\text{input}}$ 和 $\text{host.output} = M.\text{output}$ 递归独立执行子图 $M.\text{graph}$ 的阶段 1 符号表构建与阶段 2 静态类型检查（子图内部通过 `builtin:*` 或 `use` 等向 `host.output` 声明的输出变量写入值时，按阶段 2 规则满足对 $\text{host.output}[v]$ 的子类型兼容）。
3. **未挂载 `for_each` 的跨文件 `CallNode`（`use: workflow:<path>`）**：
   * 校验每个显式入参绑定 `p: val`（$p \in \text{dom}(\text{target.input})$）：若 `val` 为引用表达式 $E$，须满足子类型兼容 $\text{TypeOf}(\Sigma_G, E) <: \text{target.input}[p]$；若 `val` 为常量字面量 $L$，须满足实例兼容 $L \models \text{target.input}[p]$；
   * 校验每个显式出参绑定 `f: local_var`（$f \in \text{dom}(M.\text{with.output})$）：须满足 $\text{local\_var} \in \text{dom}(\Sigma_G)$ 且满足子类型兼容 $\text{target.output}[f] <: \Sigma_G(\text{local\_var})$（未在 $M.\text{with.output}$ 中列出的目标输出字段默认忽略）。
4. **内建算子节点（`use: builtin:<op>`）**：
   * 按 [`05-builtin-generic-inference.md`](./05-builtin-generic-inference.md) 校验所有入参引用/字面量与出参目标变量在 $\Sigma_G$ 中的已声明类型满足子类型兼容（其中每个出参变量 $v_{\text{out}}$ 必须满足 $v_{\text{out}} \in \text{dom}(\Sigma_G)$，否则报“未声明变量类型”编译期错误）。
5. **挂载了 `for_each` 的节点 $M$**：
   * **源集合校验与注入符号表构造**：校验源集合 $T_{\text{src}} = \text{TypeOf}(\Sigma_G, M.\text{for\_each.source})$ 为非空 `Array<T>` 或 `OrderedMap<string, TV>`，并按第一节构造单次迭代注入符号表 $\Sigma_{\text{injected}}(M)$。
   * **节点内部解析与 `input` 捕获校验**：
     * **若 $M$ 为 `ActionNode`（`run`）或 `CallNode`（`use: workflow:<path>`）**：定义节点 $M$ 的**单次迭代局部符号表**为 $\Sigma_{\text{iter}}(M) = \Sigma_G \uplus \Sigma_{\text{injected}}(M)$。
       * 对 `ActionNode`：校验 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_{\text{iter}}(M))$（根据 [`01-run-executor-contract.md`](../05-executor-abi/01-run-executor-contract.md)，`ActionNode` 的 `input` 必须显式列出 `run` 所需读取的 `item` / `key` / `index` 及外层变量，否则不会进入执行器的 `input_snapshot` 与 `input_schema`），并导出单次迭代的受限输入符号表 $\text{input\_schema}(M) = \Sigma_{\text{iter}}(M)|_{M.\text{input}}$；
       * 对 `CallNode`：校验 $M.\text{with.input}$ 中的每个显式入参绑定 `p: val`——若 `val` 为引用表达式 $E$，须在 $\Sigma_{\text{iter}}(M)$ 下满足子类型兼容 $\text{TypeOf}(\Sigma_{\text{iter}}(M), E) <: \text{target.input}[p]$；若 `val` 为常量字面量 $L$，须满足实例兼容 $L \models \text{target.input}[p]$。
     * **若 $M$ 为 `InlineGraphNode`（内联 `graph`）**：
       * 校验 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_G)$，且由于 $\Sigma_{\text{injected}}(M)$ 已自动注入子图初始上下文，禁止在 $M.\text{input}$ 中重复列出注入变量（即 $\text{set}(M.\text{input}) \cap \text{dom}(\Sigma_{\text{injected}}(M)) = \emptyset$）；
       * 以 $\Sigma_{\text{host}}(M.\text{graph}) = \Sigma_G|_{M.\text{input}} \uplus \Sigma_{\text{injected}}(M)$ 和**未提升前的单次迭代输出映射 $\text{host.output} = M.\text{output}$** 递归独立执行子图 $M.\text{graph}$ 的阶段 1 符号表构建与阶段 2 静态类型检查（子图内部在每次迭代到达 `end` / `end.<branch>` 时产出单次元素类型 $U = M.\text{output}[v]$）。
   * **输出目标变量一阶集合提升与子类型兼容校验**：
     记节点 $M$ 向外层绑定的**单次迭代目标类型表 $\text{SingleTargetOut}(M) : \text{RestrictedIdentifier} \rightharpoonup \text{TypeSpec}$** 为：
     * 若 $M$ 为 `ActionNode` 或 `InlineGraphNode`：$\text{SingleTargetOut}(M) = M.\text{output}$；
     * 若 $M$ 为 `CallNode`（`use: workflow:<path>`）：$\text{SingleTargetOut}(M) = \{ \text{outer\_var} \mapsto \text{target.output}[f] \mid (f, \text{outer\_var}) \in M.\text{with.output} \}$。
     对每个 $(v_{\text{out}}, U) \in \text{SingleTargetOut}(M)$，校验 $v_{\text{out}} \in \text{dom}(\Sigma_G)$，且经 `for_each` 屏障汇聚后的一阶同构提升集合类型满足对当前图目标变量类型的单向子类型兼容：
     $$\text{Lift}(T_{\text{src}}, U) <: \Sigma_G(v_{\text{out}})$$
