# 04. 并发写集合两两互斥分析（Concurrent Disjoint Write Analysis）

为在编译期消除并发写-写竞争（Write-Write Race），静态检查器对所有 Fork-Join 异构并发组执行写集合互斥校验。

---

## 一、 Fork-Join 并发组写集合互斥公理

对于任意三元并发边 `src -> [W_1, W_2, ..., W_k] -> join_target`，记每个并发成员节点 $W_i$（无论是否挂载 `for_each`）向所属父图 `variables` 提交的顶层目标变量集合为 $OutVars(W_i)$（其形式化定义见 [03. 全路径分支敏感必达定值分析 - 二、边级定值传递方程](./03-branch-sensitive-definite-assignment.md)）：

* **跨栈帧写集边界隔离（Cross-Frame Write Isolation）**：
  若并发成员 $W_i$ 为 `InlineGraphNode`（内联 `graph`）或 `CallNode`（`use`），其子栈帧内部私有局部变量（`graph.variables` 或被调用工作流内部 `variables`）的写入**不计入**父图视角的 $OutVars(W_i)$。因此，不同并发分支的子图或子工作流内部允许合法使用同名私有局部变量，互斥检查严格作用于提交回当前父图 `variables` 的顶层输出集合 $OutVars(W_i)$。
* **两两互斥断言（Pairwise Disjointness）**：
  $$\forall i, j \in \{1, \dots, k\}, \quad i \neq j \implies OutVars(W_i) \cap OutVars(W_j) = \emptyset$$
  若存在任意变量 $v \in OutVars(W_i) \cap OutVars(W_j)$，编译期报错。
