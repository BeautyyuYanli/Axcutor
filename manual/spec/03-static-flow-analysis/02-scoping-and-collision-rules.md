# 02. 宿主初值互斥与局部帧命名无冲突规则

本规范定义编译期针对 `graph.variables` 初值声明、`for_each` 局部帧注入变量名无冲突，以及三类节点在挂载 `for_each` 时的 `input` 捕获校验准则。

---

## 一、 宿主上下文输入与 `graph.variables` 初值互斥公理

定义图 $G$ 的**宿主上下文输入变量集合 $\text{HostIn}(G)$**：

* 若 $G$ 为根节点的图：
  $$\text{HostIn}(G) = \text{dom}(\text{root.input})$$
* 若 $G$ 为内联 `graph` 节点 $N$ 的图：
  $$\text{HostIn}(G) = \text{set}(N.\text{input}) \cup \text{Injected}(N.\text{for\_each}) \cup \text{set}(N.\text{for\_each.state})$$
  *(其中 $\text{Injected}(N.\text{for\_each}) = \text{dom}(\Sigma_{\text{injected}}(N)) = \{N.\text{for\_each.item}\} \cup \{N.\text{for\_each.key}\text{ (若声明)}\} \cup \{N.\text{for\_each.index}\text{ (若声明)}\}$。)*

**互斥公理**：
$$\text{HostIn}(G) \cap \text{dom}(G.\text{variables}) = \emptyset$$
凡属于 $\text{HostIn}(G)$ 的变量在进入图 $G$ 时已持有外层赋予的确定运行期值，禁止在 $G.\text{variables}$ 中重复声明初值，否则编译期报错。

---

## 二、 `for_each` 局部帧注入名无冲突公理

对于任意挂载了 `for_each` 的节点 $M$（位于外层图 $G_{\text{outer}}$ 中，外层图符号表为 $\Sigma_{\text{outer}}$，单次迭代注入符号表 $\Sigma_{\text{injected}}(M)$ 定义见 [`03-two-phase-symbol-resolution.md`](../02-type-system-and-symbols/03-two-phase-symbol-resolution.md)）：

1. **注入项内部互异**：
   `for_each` 声明的 `item`、`key`（若有）、`index`（若有）三者标识符必须两两互不相同。
2. **当 $M$ 为 `ActionNode`（`run`）或 `CallNode`（`use`）时**：
   * 注入项集合 $\text{Injected}(M.\text{for\_each})$ 禁止与外层图符号表中的任何已有变量重名：
     $$\text{Injected}(M.\text{for\_each}) \cap \text{dom}(\Sigma_{\text{outer}}) = \emptyset$$
   * 由此保证单次迭代局部符号表 $\Sigma_{\text{iter}}(M) = \Sigma_{\text{outer}} \uplus \Sigma_{\text{injected}}(M)$ 的无冲突良构性。
3. **当 $M$ 为 `InlineGraphNode`（内联 `graph`）时**：
   * 注入项集合禁止与该节点显式捕获的 `input`、`for_each.state` 以及该子图对外声明的单次 `output` 字段重名：
     $$\text{Injected}(M.\text{for\_each}) \cap (\text{set}(M.\text{input}) \cup \text{set}(M.\text{for\_each.state}) \cup \text{dom}(M.\text{output})) = \emptyset$$
4. **单次非 `state` 产出与外层图变量无遮蔽**：
   * 挂载了 `for_each` 的节点 $M$，其单次迭代产出中未列入 `for_each.state` 的局部字段名 `<single_field>`（待由 `collect` 提升），禁止与外层图符号表 $\Sigma_{\text{outer}}$ 中的变量同名（包含不可与 `collect` 映射的目标集合变量 `<target_var>` 同名）。

---

## 三、 挂载 `for_each` 时三类节点的 `input` 捕获与内部引用校验准则

由于 `ActionNode`（`run`）、`CallNode`（`use: workflow:<path>`）与 `InlineGraphNode`（内联 `graph`）在运行期拥有不同的栈帧与输入快照构造契约，三者在挂载 `for_each` 时的 `input` 校验规则严格区分如下：

| 节点类型 | 内部解析符号表 | `input` / `with.input` 校验约束 |
| :--- | :--- | :--- |
| **`ActionNode`（`run`）** | $\Sigma_{\text{iter}}(M) = \Sigma_{\text{outer}} \uplus \Sigma_{\text{injected}}(M)$ | • $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_{\text{iter}}(M))$；<br>• `M.input` **必须显式列出** `run` 所需读取的 `item` / `key` / `index`、`state` 及外层变量（未列入 `M.input` 的变量不进入传给执行器的 `input_snapshot`）；<br>• `run` 内所有 `variables.<E>` 引用在受限符号表 $\Sigma_{\text{iter}}(M)|_{M.\text{input}}$ 下通过 `TypeOf` 校验。 |
| **`CallNode`（`use: workflow:<path>`）** | $\Sigma_{\text{iter}}(M) = \Sigma_{\text{outer}} \uplus \Sigma_{\text{injected}}(M)$ | • `M.with.input`（含默认同名补全项）中的所有变量或路径引用在 $\Sigma_{\text{iter}}(M)$ 下通过 `TypeOf` 校验。 |
| **`InlineGraphNode`（内联 `graph`）** | $\Sigma_{\text{host}}(M) = \Sigma_{\text{outer}}|_{M.\text{input} \cup M.\text{for\_each.state}} \uplus \Sigma_{\text{injected}}(M)$ | • $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_{\text{outer}})$；<br>• 由于 $\Sigma_{\text{injected}}(M)$ 与 `for_each.state` 已自动注入子图初始 `BlockFrame`，**严禁**在 `M.input` 列表中重复列出这些变量：<br>$\text{set}(M.\text{input}) \cap (\text{dom}(\Sigma_{\text{injected}}(M)) \cup \text{set}(M.\text{for\_each.state})) = \emptyset$。 |
