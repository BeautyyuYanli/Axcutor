# 02. 宿主初值互斥与局部帧命名无冲突规则

本规范定义编译期针对 `graph.variables` 初值声明以及 `for_each` 局部帧注入变量名的无冲突校验准则。

---

## 一、 宿主上下文输入与 `graph.variables` 初值互斥公理

定义图 $G$ 的**宿主上下文输入变量集合 $\text{HostIn}(G)$**：

* 若 $G$ 为根节点的图：
  $$\text{HostIn}(G) = \text{dom}(\text{root.input})$$
* 若 $G$ 为内联 `graph` 节点 $N$ 的图：
  $$\text{HostIn}(G) = \text{set}(N.\text{input}) \cup \text{Injected}(N.\text{for\_each}) \cup \text{set}(N.\text{for\_each.state})$$
  *(其中 $\text{Injected}(N.\text{for\_each}) = \{N.\text{for\_each.item}\} \cup \{N.\text{for\_each.key}\text{ (若声明)}\} \cup \{N.\text{for\_each.index}\text{ (若声明)}\}$。)*

**互斥公理**：
$$\text{HostIn}(G) \cap \text{dom}(G.\text{variables}) = \emptyset$$
凡属于 $\text{HostIn}(G)$ 的变量在进入图 $G$ 时已持有外层赋予的确定运行期值，禁止在 $G.\text{variables}$ 中重复声明初值，否则编译期报错。

* 此外，对于内联 `graph` 节点 $N$，由于 $\text{Injected}(N.\text{for\_each})$ 与 $N.\text{for\_each.state}$ 已自动注入子图初始上下文，禁止在 $N.\text{input}$ 列表中重复列出这些变量：
  $$\text{set}(N.\text{input}) \cap (\text{Injected}(N.\text{for\_each}) \cup \text{set}(N.\text{for\_each.state})) = \emptyset$$

---

## 二、 `for_each` 局部帧注入名无冲突公理

对于任意挂载了 `for_each` 的节点 $M$（位于外层图 $G_{\text{outer}}$ 中，外层图符号表为 $\Sigma_{\text{outer}}$）：

1. **注入项内部互异**：
   `for_each` 声明的 `item`、`key`（若有）、`index`（若有）三者标识符必须两两互不相同。
2. **当 $M$ 为 `ActionNode`（`run`）或 `CallNode`（`use`）时**：
   * 注入项集合 $\text{Injected}(M.\text{for\_each})$ 禁止与外层图符号表中的任何已有变量重名：
     $$\text{Injected}(M.\text{for\_each}) \cap \text{dom}(\Sigma_{\text{outer}}) = \emptyset$$
3. **当 $M$ 为 `InlineGraphNode`（内联 `graph`）时**：
   * 注入项集合禁止与该节点显式捕获的 `input`、`for_each.state` 以及该子图对外声明的单次 `output` 字段重名：
     $$\text{Injected}(M.\text{for\_each}) \cap (\text{set}(M.\text{input}) \cup \text{set}(M.\text{for\_each.state}) \cup \text{dom}(M.\text{output})) = \emptyset$$
4. **单次非 `state` 产出与外层图变量无遮蔽**：
   * 挂载了 `for_each` 的节点 $M$，其单次迭代产出中未列入 `for_each.state` 的局部字段名 `<single_field>`（待由 `collect` 提升），禁止与外层图符号表 $\Sigma_{\text{outer}}$ 中的变量同名（包含不可与 `collect` 映射的目标集合变量 `<target_var>` 同名）。
