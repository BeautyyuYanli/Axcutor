# 02. 宿主输入互斥与局部帧命名无冲突规则

本规范定义编译期关于**作用域边界与标识符无冲突的静态公理（Scoping & Non-Collision Axioms）**，涵盖宿主上下文输入与 `graph.variables` 互斥、`for_each` 注入变量名互异且与外层/子图边界无冲突，以及内联 `graph` 捕获列表 `input` 与 `for_each` 注入项不重叠公理。关于各节点在对应符号表下的 `TypeOf` 引用解析与静态类型检查规则，详见 [`03-two-phase-symbol-resolution.md`](../02-type-system-and-symbols/03-two-phase-symbol-resolution.md)。

---

## 一、 宿主上下文输入与 `graph.variables` 互斥公理

定义图 $G$ 的**宿主上下文输入变量集合 $\text{HostIn}(G)$**：

* 若 $G$ 为根节点的图：
  $$\text{HostIn}(G) = \text{dom}(\text{root.input})$$
* 若 $G$ 为内联 `graph` 节点 $N$ 的图：
  $$\text{HostIn}(G) = \text{set}(N.\text{input}) \cup \text{Injected}(N.\text{for\_each})$$
  *(其中 $\text{Injected}(N.\text{for\_each}) = \text{dom}(\Sigma_{\text{injected}}(N)) = \{N.\text{for\_each.item}\} \cup \{N.\text{for\_each.key}\text{ (若声明)}\} \cup \{N.\text{for\_each.index}\text{ (若声明)}\}$。)*

**互斥公理**：
$$\text{HostIn}(G) \cap \text{dom}(G.\text{variables}) = \emptyset$$
凡属于 $\text{HostIn}(G)$ 的变量已在宿主输入契约中拥有明确类型与初始运行期值，禁止在 $G.\text{variables}$ 中重复声明类型或初值，否则编译期报错。

---

## 二、 `for_each` 注入变量名互异与作用域边界无冲突公理

对于任意挂载了 `for_each` 的节点 $M$（位于外层图 $G_{\text{outer}}$ 中，外层图符号表为 $\Sigma_{\text{outer}}$，单次迭代注入符号表 $\Sigma_{\text{injected}}(M)$ 定义见 [`03-two-phase-symbol-resolution.md`](../02-type-system-and-symbols/03-two-phase-symbol-resolution.md)）：

1. **注入项内部互异**：
   `for_each` 声明的 `item`、`key`（若有）、`index`（若有）三者标识符必须两两互不相同。
2. **当 $M$ 为 `ActionNode`（`run`）或 `CallNode`（`use`）时（外层作用域无冲突）**：
   * 注入项集合 $\text{Injected}(M.\text{for\_each})$ 禁止与外层图符号表中的任何已有变量重名：
     $$\text{Injected}(M.\text{for\_each}) \cap \text{dom}(\Sigma_{\text{outer}}) = \emptyset$$
   * 由此保证单次迭代局部符号表 $\Sigma_{\text{iter}}(M) = \Sigma_{\text{outer}} \uplus \Sigma_{\text{injected}}(M)$ 的不相交并集（Disjoint Union）良构性。
3. **当 $M$ 为 `InlineGraphNode`（内联 `graph`）时（子图边界无冲突）**：
   * 注入项集合禁止与该节点显式捕获的 `input` 以及该子图对外声明的单次 `output` 字段重名：
     $$\text{Injected}(M.\text{for\_each}) \cap (\text{set}(M.\text{input}) \cup \text{dom}(M.\text{output})) = \emptyset$$

---

## 三、 内联 `graph` 捕获列表 `input` 与 `for_each` 注入项不重叠公理

在挂载 `for_each` 时，内联 `graph`（`InlineGraphNode`）与执行器节点（`ActionNode`）的 `input` 列表承担不同的作用域捕获职责，遵循以下边界公理：

1. **内联 `graph` 捕获列表与 `for_each` 注入项不重叠**：
   * 内联子图节点 $M$ 的 `M.input` 仅用于从外层图符号表 $\Sigma_{\text{outer}}$ 显式捕获外层变量（要求 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_{\text{outer}})$）。
   * 由于 $\text{Injected}(M.\text{for\_each})$ 已按第一节规则自动并入子图的宿主输入集合 $\text{HostIn}(M.\text{graph})$，**严禁**在 `M.input` 列表中重复声明 `for_each` 注入变量：
     $$\text{set}(M.\text{input}) \cap \text{Injected}(M.\text{for\_each}) = \emptyset$$
2. **与 `ActionNode`（`run`）显式快照白名单的边界区分**：
   * 与内联子图自动注入不同，`ActionNode` 的 `M.input` 直接定义了传给执行器的 `input_snapshot` 唯一白名单边界（要求 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_{\text{iter}}(M))$）；若 `run` 需读取 `for_each` 的 `item` / `key` / `index` 或外层变量，均必须在 `M.input` 中显式列出。
3. **引用解析与 `TypeOf` 类型检查交叉引用**：
   * 在上述作用域与无冲突公理确立后，`ActionNode`、`CallNode` 与 `InlineGraphNode` 内部引用及参数绑定的 `TypeOf` 静态类型检查规则，统一遵循 [`03-two-phase-symbol-resolution.md`](../02-type-system-and-symbols/03-two-phase-symbol-resolution.md) 第二节之规定。
