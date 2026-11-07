# 04. 并发写集合两两互斥分析（Concurrent Disjoint Write Analysis）

为在编译期消除并发写-写竞争（Write-Write Race），静态检查器对所有 Fork-Join 异构并发组执行写集合互斥校验。

---

## 一、 Fork-Join 并发组写集合互斥公理

对于任意三元并发边 `src -> [W_1, W_2, ..., W_k] -> join_target`：

* 记每个并发成员节点 $W_i$（无论是否挂载 `for_each`）向所属外层图 `variables` 提交的顶层目标变量集合为 $OutVars(W_i)$：
  * 当 $W_i$ 为 `ActionNode` 或 `InlineGraphNode` 时：$OutVars(W_i) = \text{dom}(W_i.\text{output})$；
  * 当 $W_i$ 为 `CallNode` 时：$OutVars(W_i) = \text{values}(W_i.\text{with.output})$。
* **两两互斥断言（Pairwise Disjointness）**：
  $$\forall i, j \in \{1, \dots, k\}, \quad i \neq j \implies OutVars(W_i) \cap OutVars(W_j) = \emptyset$$
  若存在任意变量 $v \in OutVars(W_i) \cap OutVars(W_j)$，编译期报错。
