# 03. 图级静态符号表两阶段决议算法（Two-Phase Symbol Resolution）

运行时在编译期按**从外层图到内层子图（Top-Down）**的顺序，为每一张 `graph` 独立构建静态符号表 $\Sigma : \text{Identifier} \rightharpoonup \text{TypeSpec}$。

---

## 一、 第一阶段：收集显式/推导 `TypeSpec` 并验证同帧等价铁律

对于当前图 $G$，收集以下四类来源中出现的所有 `(var_name, TypeSpec)` 对：

1. **宿主上下文输入（Host Context Inputs）**：
   * 若 $G$ 为根节点的图：对每个 $(v, T) \in \text{root.input}$，加入 $(v, T)$。
   * 若 $G$ 为内联子图节点 $N$ 的图：
     * 对每个 $v \in N.\text{input}$（显式捕获列表），从外层父图符号表 $\Sigma_{\text{parent}}$ 提取 $(v, \Sigma_{\text{parent}}(v))$ 加入（若 $v \notin \text{dom}(\Sigma_{\text{parent}})$ 则编译期报错）；
     * 若 $N$ 挂载了 `for_each`，将拆包注入项 `item`（及可选的 `key`、`index`）及其从 $\text{Type}(N.\text{for\_each.source})$ 推导出的类型加入；
     * 若 $N$ 声明了 `for_each.state`，对每个 $s \in N.\text{for\_each.state}$，从 $\Sigma_{\text{parent}}$ 提取 $(s, \Sigma_{\text{parent}}(s))$ 加入。
2. **`G.variables` 中显式带类型的声明项**：
   * 对 `G.variables` 中形如 `<v>: { <TypeSpec>, default: <literal> }` 的项，校验 `<literal> \models <TypeSpec>` 后，将 $(v, \text{TypeSpec})$ 加入。
3. **宿主 `output` 与图内非 `for_each` 节点的输出**：
   * 若 $G$ 为根节点的图，将 $\text{root.output}$ 中的 $(v, T)$ 加入；若 $G$ 为内联子图节点 $N$ 且 $N.\text{output}$ 采用完整映射形式 `{ <v>: <TypeSpec> }`，将其加入。
   * 对图内每个未挂载 `for_each` 的子节点 $M$：
     * 若 $M$ 为 `ActionNode`（`run`）：将 $M.\text{output}$ 加入；
     * 若 $M$ 为 `InlineGraphNode`（`graph`）：先递归完成 $M$ 内部子图符号表 $\Sigma_M$ 的决议；若 $M.\text{output}$ 为列表简写 $[v_1, \dots, v_m]$，则从 $\Sigma_M$ 提取每个 $(v_i, \Sigma_M(v_i))$ 加入当前图符号表（若 $v_i \notin \text{dom}(\Sigma_M)$ 则报错）；若 $M.\text{output}$ 为完整映射，则校验与 $\Sigma_M(v_i)$ 严格等价后加入；
     * 若 $M$ 为 `CallNode`（`use`）：将目标工作流 `output`（或 `builtin:*` 泛型推导输出）经 $M.\text{with.output}$ 映射后的 `(local_var, T)` 加入。
4. **图内 `for_each` 节点的三通道外层目标变量**：
   * 对挂载了 `for_each` 的子节点 $M$，其单次迭代局部产出仅用于内部推导，**绝不加入外层图符号表 $\Sigma$**；仅将其 `filter`、`collect`、`state` 映射到的外层目标变量及提升后的集合类型加入 $\Sigma$：
     * **`filter` 目标变量**：类型等于 $\text{Type}(M.\text{for\_each.source})$；
     * **`collect` 目标变量（`flatten: false`）**：设单次字段类型为 $U$，当源为 `Array<T>` 时提升为 `Array<U>`，当源为 `OrderedMap<string, TV>` 时提升为 `OrderedMap<string, U>`；
     * **`collect` 目标变量（`flatten: true`）**：要求源为 `Array<T>` 且单次字段类型为 `Array<U>`，提升后为一维 `Array<U>`；
     * **`state` 目标变量**：要求已在当前图第一阶段获得类型且与单次同名产出字段类型严格等价。

> **同帧同名同类型铁律（Same-Frame Type Invariance）**：
> 在第一阶段收集完毕后，对于任意变量名 $v$，若它在上述来源中出现了多个类型声明 $T_1, T_2, \dots, T_k$，必须满足两两静态等价 $T_1 \equiv T_2 \equiv \dots \equiv T_k$，否则编译期直接报错。由此唯一确定 $\Sigma(v) = T_1$。

---

## 二、 第二阶段：`G.variables` 字面量简写决议与标量兜底推导

扫描 `G.variables` 中使用字面量简写 `<v>: <literal>` 的每一项：
1. **若 $v \in \text{dom}(\Sigma)$（已在第一阶段获得目标类型 $T = \Sigma(v)$）**：
   * 执行实例兼容性校验 $\text{literal} \models T$；若不兼容则编译期报错。
2. **若 $v \notin \text{dom}(\Sigma)$（未在任何显式 `TypeSpec` 处出现过的纯内部变量）**：
   * `<literal>` **必须为非空基础标量字面量**（`string` / `integer` / `float` / `boolean`）；
   * 自动将其类型推导为对应的无 `enum` 非空基础标量 `TypeSpec` 并注册进 $\Sigma(v)$。
   * 若 `<literal>` 为 `null`、空数组 `[]` 或对象 `{}` 且 $v \notin \text{dom}(\Sigma)$，编译期直接报错（必须改用显式带类型声明 `{ <TypeSpec>, default: <literal> }`）。
