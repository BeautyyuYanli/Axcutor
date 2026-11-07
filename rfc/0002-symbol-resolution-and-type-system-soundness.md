# RFC-0002: 符号表不动点决议、`for_each` 局部符号表与类型投影完备性

* **状态**：Implemented
* **所属分类**：形式化规范完备性与自洽性修正（Formal Specification Soundness & Completeness）
* **核心解决的问题**：解决两阶段符号表决议中 Phase 1 与 Phase 2 的依赖倒置死锁；补全 `ActionNode` / `CallNode` 的单次迭代符号表 $\Sigma_{\text{iter}}$；修复 `TypedDict` 非必填字段无法投影访问的死锁；补充 `builtin:switch` 枚举合法标识符校验。
* **关联规范模块**：
  * [`02-type-system-and-symbols/01-typespec-well-formedness.md`](../manual/spec/02-type-system-and-symbols/01-typespec-well-formedness.md)
  * [`02-type-system-and-symbols/03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md)
  * [`02-type-system-and-symbols/04-static-field-projection-typing.md`](../manual/spec/02-type-system-and-symbols/04-static-field-projection-typing.md)
  * [`02-type-system-and-symbols/05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md)
  * [`03-static-flow-analysis/02-scoping-and-collision-rules.md`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md)

---

## 一、 问题背景与缺陷分析

### 问题 2.1：两阶段符号表决议算法（Two-Phase Symbol Resolution）存在依赖顺序死锁
在 [`03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md) 中，符号表构建被划分为两个线性阶段：
* **第一阶段**：收集 `HostIn(G)`、`G.variables` 中显式带 `TypeSpec` 的项、`host.output`、图内非 `for_each` 子节点的输出（含递归决议内联子图 $\Sigma_M$ 并提取 `output: [v_1, ...]` 列表简写、推导 `builtin:*` 泛型输出），以及 `for_each` 三通道外层目标变量类型；
* **第二阶段**：扫描 `G.variables` 中使用字面量简写 `<v>: <literal>` 的项，若 $v \notin \text{dom}(\Sigma)$ 则推导为基础标量类型并加入 $\Sigma$。

该算法在以下三类完全合法的常见工作流中会发生**编译期决议死锁或误报错**：

1. **Phase 1 对 Phase 2 标量简写变量的跨阶段依赖**：
   ```yaml
   graph:
     variables:
       # 字面量简写：按现行规范要到 Phase 2 才会进入符号表 Σ_parent
       max_retry_limit: 3
       is_dry_run: false
     nodes:
       route_mode:
         # Phase 1 推导 builtin:switch 时需要查 Σ_parent(is_dry_run)，但此时尚未进入 Phase 2！
         use: builtin:switch
         with:
           input:
             source: is_dry_run
       worker_block:
         # Phase 1 递归决议内联子图 Σ_M 时需要从 Σ_parent 提取 max_retry_limit，此时 Σ_parent 中尚无该项！
         input: [max_retry_limit]
         output: [final_count]
         graph: ...
   ```
2. **Phase 1 内部的链式类型推导依赖**：
   * 节点 A（`builtin:unwrap`）读取 `opt_items`（类型 `[array, "null"]`），推导产出 `clean_items`（类型 `array`）；
   * 节点 B（挂载 `for_each: { source: clean_items, item: x, collect: { out: results } }` 的内联 `graph`，且使用列表简写 `output: [out]`）需要先知道 `clean_items` 的类型才能确定注入内联子图的 `x` 的类型，进而决议内联子图得到 `out` 的类型，最后由 `collect` 提升得到 `results` 的类型；
   * 节点 C（`builtin:array_shift`）读取 `results` 推导 `first_item` 的类型。
   * 单遍无序收集无法处理 `节点 A -> 节点 B (for_each in) -> 子图 M -> 节点 B (for_each out) -> 节点 C` 的跨节点类型推导链。

---

### 问题 2.2：`ActionNode` 与 `CallNode` 挂载 `for_each` 时缺失单次迭代局部符号表 $\Sigma_{\text{iter}}(M)$
* [`03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md) 仅在 `InlineGraphNode` 的子图构建中加入了 `for_each` 注入变量 `item`、`key`、`index`。
* 当 `ActionNode`（`run`）或 `CallNode`（`use: workflow:<path>`）挂载 `for_each` 时：
  * `item`、`key`、`index` 并不属于外层图符号表 $\Sigma_{\text{outer}}$；
  * 但 `ActionNode` 的 `input: [item_idx, current_item]`、`run` 内部的 `variables.current_item.title`，以及 `CallNode` 的 `with.input.doc: current_item.body` 都必须对 `current_item` 执行静态类型查找与字段路径投影 [`TypeOf(\Sigma, E)`](../manual/spec/02-type-system-and-symbols/04-static-field-projection-typing.md)！
  * 现行规范缺失了为挂载 `for_each` 的节点 $M$ 构造**单次迭代局部符号表 $\Sigma_{\text{iter}}(M)$** 的形式化步骤。
* 此外，关于 `for_each` 注入变量与 `state` 变量是否出现在 `node.input` 中，现行规范在 `ActionNode` 与 `InlineGraphNode` 之间存在隐式不对称：
  * 在 `InlineGraphNode` 中，[`02-scoping-and-collision-rules.md`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md#L22-L23) **严禁** `node.input` 列出 `item`/`key`/`index`/`state`；
  * 在 `ActionNode` 中，`node.input` **必须**列出 `run` 所需读取的 `item`/`key`/`index`/`state`，否则根据 [`01-run-executor-contract.md`](../manual/spec/05-executor-abi/01-run-executor-contract.md#L25)，未列入 `input` 的变量不会进入传给执行器的 `input_snapshot`。这一差异必须在规范中显式声明并给出各自的校验集合公式。

---

### 问题 2.3：`TypedDict` 的非必填字段（`required` 真子集）在静态路径投影下彻底无法访问
* [`01-typespec-well-formedness.md`](../manual/spec/02-type-system-and-symbols/01-typespec-well-formedness.md#L45-L47) 允许 `TypedDict` 声明 `required` 为 `dom(properties)` 的子集（即允许存在可选属性 $f \notin \text{set}(\text{required})$）。
* 然而，[`04-static-field-projection-typing.md`](../manual/spec/02-type-system-and-symbols/04-static-field-projection-typing.md#L21) 第 2.3 条对路径 $E = v_0.f_1\dots f_k$ 的**每一步 $i \in \{1, \dots, k\}$** 均强制断言：
  $$f_i \in \text{dom}(T_{i-1}.\text{properties}) \land f_i \in \text{set}(T_{i-1}.\text{required})$$
* 这导致一个严重悖论：若某结构体字段 $f$ 未列入 `required`，用户甚至无法写出 `with: { input: { source: user.nickname } }` 将其传给 `builtin:unwrap` 进行判空拆包，因为 `TypeOf(Sigma, user.nickname)` 在第一步就会因 `nickname` 不在 `required` 中而直接报编译期错误！非 `required` 字段在语言层面成为了“允许声明但永远无法读取”的死特征。

---

### 问题 2.4：`builtin:switch` 未校验 `enum` 元素是否为合法分支标识符
* [`05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md#L22-L32) 允许 `builtin:switch` 接收任意带非空 `enum: [e_1, ..., e_m]` 的 `string` 类型变量，并直接生成分支 `.e_1, ..., .e_m`。
* 但 [`01-typespec-well-formedness.md`](../manual/spec/02-type-system-and-symbols/01-typespec-well-formedness.md#L25-L27) 中的 `string` `enum` 允许任意字符串（例如 `"in-progress"`、`"step.1"`、`"start"`、`"end"`、`"fail"` 或 `""`），此类字符串作为边语法 `node.<branch>` 的后缀时，会直接违反 [`03-edge-and-cfg-grammar.md`](../manual/spec/01-grammar-and-ast/03-edge-and-cfg-grammar.md) 的 `Identifier` 词法规则或保留字禁令。

---

## 二、 解决方案与规范修订设计

### 方案 2.1：将两阶段符号表决议升级为“三步工作队列不动点决议算法（Worklist Symbol Resolution）”

重构 [`03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md) 的符号表构建流程：

1. **阶段 1（无依赖显式类型收集与 `variables` 孤立标量预推导）**：
   * 收集所有无需依赖其他节点推导结果的显式 `(var_name, TypeSpec)` 对：
     * 根图的 `root.input`、`root.output`；
     * `G.variables` 中显式带 `TypeSpec` 的声明项；
     * 图内未挂载 `for_each` 的 `ActionNode.output`、`InlineGraphNode.output`（当为完整映射 `{ <v>: <TypeSpec> }` 时）、`CallNode`（当为 `use: workflow:<path>` 时，直接读取被调用工作流顶层显式声明的 `output` 经 `with.output` 映射后的类型）。
   * **孤立标量字面量预登録**：扫描 `G.variables` 中使用字面量简写 `<v>: <literal>` 的项。若变量名 $v$ **未出现在**当前图内任何节点的写入目标位置（即不属于任何节点的 `output`、`with.output`、`for_each.filter`、`for_each.collect`）且不属于目前已收集的显式类型集，则立即校验 `<literal>` 为非空基础标量，将其推导为对应标量 `TypeSpec` 并加入 $\Sigma_G$。
2. **阶段 2（工作队列迭代推导：处理 `builtin:*`、`for_each` 与内联子图列表简写）**：
   * 将所有待推导项（`use: builtin:*` 节点、挂载了 `for_each` 的节点、使用 `output: [v_1, ...]` 列表简写的 `InlineGraphNode`）放入工作队列 $Q$；
   * 循环扫描 $Q$：
     * 若某项依赖的输入根变量均已在 $\text{dom}(\Sigma_G)$ 中获得类型，则执行其类型推导（对 `InlineGraphNode`，此时其 `input`、`for_each` 源与 `state` 在 $\Sigma_G$ 中均已就绪，可递归完整运行子图符号表决议算法得到 $\Sigma_M$，再提取 `output` 列表简写类型），将推导出的新 `(var_name, TypeSpec)` 并入 $\Sigma_G$（若已存在则校验静态等价 $T_1 \equiv T_2$），并将该项移出 $Q$；
   * 若某一轮迭代中 $Q$ 非空但无任何项可推进，则说明存在循环类型推导依赖或引用了未声明变量，编译期报错。
3. **阶段 3（`G.variables` 剩余字面量简写兼容性校验与兜底推导）**：
   * 对 `G.variables` 中其余字面量简写项 `<v>: <literal>`：
     * 若 $v \in \text{dom}(\Sigma_G)$，校验 $\text{literal} \models \Sigma_G(v)$；
     * 若 $v \notin \text{dom}(\Sigma_G)$，按非空基础标量推导并注册至 $\Sigma_G$。

---

### 方案 2.2：形式化定义 `for_each` 单次迭代局部符号表 $\Sigma_{\text{iter}}(M)$ 与节点 `input` 校验规则

在 [`03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md) 与 [`02-scoping-and-collision-rules.md`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md) 中补充：

1. **单次迭代注入符号表 $\Sigma_{\text{injected}}(M)$**：
   设 $T_{\text{src}} = \text{TypeOf}(\Sigma_{\text{outer}}, M.\text{for\_each.source})$：
   * 当 $T_{\text{src}}$ 为 `{ type: array, items: T_elem }` 时：
     $$\Sigma_{\text{injected}}(M) = \{ M.\text{for\_each.item} \mapsto T_{\text{elem}} \} \cup \{ M.\text{for\_each.index} \mapsto \{\texttt{type}: \texttt{integer}\} \text{ (若声明)} \}$$
   * 当 $T_{\text{src}}$ 为 `{ type: object, additionalProperties: T_val }` 时：
     $$\Sigma_{\text{injected}}(M) = \{ M.\text{for\_each.item} \mapsto T_{\text{val}} \} \cup \{ M.\text{for\_each.key} \mapsto \{\texttt{type}: \texttt{string}\} \text{ (若声明)} \} \cup \{ M.\text{for\_each.index} \mapsto \{\texttt{type}: \texttt{integer}\} \text{ (若声明)} \}$$
2. **三类节点的内部解析符号表**：
   * **当 $M$ 为挂载了 `for_each` 的 `ActionNode` 或 `CallNode` 时**：
     定义单次迭代符号表 $\Sigma_{\text{iter}}(M) = \Sigma_{\text{outer}} \uplus \Sigma_{\text{injected}}(M)$（无冲突性由 `dom(\Sigma_outer) \cap dom(\Sigma_injected) = \emptyset` 保证）。
     * 对 `ActionNode`：要求 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_{\text{iter}}(M))$，且 `run` 内部所有 `variables.<E>` 在 $\Sigma_{\text{iter}}(M)|_{M.\text{input}}$ 下通过 `TypeOf` 校验；
     * 对 `CallNode`：要求 `with.input` 中所有变量/路径引用在 $\Sigma_{\text{iter}}(M)$ 下通过 `TypeOf` 校验。
   * **当 $M$ 为挂载了 `for_each` 的 `InlineGraphNode` 时**：
     子图初始宿主输入符号表为 $\Sigma_{\text{host}}(M) = \Sigma_{\text{outer}}|_{M.\text{input} \cup M.\text{for\_each.state}} \uplus \Sigma_{\text{injected}}(M)$，其中要求 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_{\text{outer}})$ 且 $\text{set}(M.\text{input}) \cap (\text{dom}(\Sigma_{\text{injected}}(M)) \cup \text{set}(M.\text{for\_each.state})) = \emptyset$。

---

### 方案 2.3：修复 `TypedDict` 非必填字段的静态路径投影规则

修改 [`04-static-field-projection-typing.md`](../manual/spec/02-type-system-and-symbols/04-static-field-projection-typing.md) 第 2 节（逐级非空结构体穿透校验）：

对于路径表达式 $E = v_0 . f_1 . f_2 \dots f_k$（$k \ge 1$），在每一步 $i = 1, 2, \dots, k$：
1. **前缀非空与 `TypedDict` 检查**：$T_{i-1}$ 必须满足 $\text{Nullable}(T_{i-1}) = \texttt{false}$ 且为 `TypedDict`（含 `properties`），且 $f_i \in \text{dom}(T_{i-1}.\text{properties})$。
2. **区分中间穿透步（$i < k$）与叶子投影步（$i = k$）的必填性处理**：
   * **若 $f_i \in \text{set}(T_{i-1}.\text{required})$**：令 $T_i = T_{i-1}.\text{properties}[f_i]$；
   * **若 $f_i \notin \text{set}(T_{i-1}.\text{required})$（可选属性）**：
     * 当 $i < k$（试图继续向下穿透 `.f_{i+1}`）时：直接报编译期错误（禁止对可能缺失的非必填属性直接穿透，正如禁止对可空结构体穿透一样）；
     * 当 $i = k$（作为路径的最末级叶子属性）时：令最终推导类型为显式可空提升类型：
       $$T_k = \text{MakeNullable}(T_{k-1}.\text{properties}[f_k])$$
       并在运行期取值时，若对象中不存在键 $f_k$，统一求值为 `null`。

> **效果**：`TypedDict` 中未列入 `required` 的可选字段 $f_k$ 在被投影访问（`user.nickname`）时，编译期静态类型自动推导为 `[BaseType, "null"]`，既阻止了将其直接赋给非空目标参数，又允许将其传入 `builtin:unwrap`（`source: user.nickname`）安全拆包为非空值，彻底消除了死特征。

---

### 方案 2.4：补充 `builtin:switch` 的枚举标识符良构性校验

在 [`05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md) 第 一.2 节（`builtin:switch` 入参校验）中增加编译期约束：
* 当 $\text{BaseType}(T_{\text{src}}) = \texttt{string}$ 且声明了 `enum: [e_1, ..., e_m]` 时，必须额外满足：
  $$\forall e_i \in T_{\text{src}}.\text{enum}, \quad e_i \text{ 匹配 } \texttt{^[a-zA-Z\_][a-zA-Z0-9\_]*\$} \;\land\; e_i \notin \{\texttt{"start"}, \texttt{"end"}, \texttt{"fail"}\}$$
* 若存在任何枚举值不是合法的 `BranchId`，静态检查器在编译期报错。
