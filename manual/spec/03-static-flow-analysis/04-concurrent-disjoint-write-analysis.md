# 04. 并发写集合两两互斥分析（Concurrent Disjoint Write Analysis）

为在编译期消除并发写-写竞争（Write-Write Race），静态检查器对所有 Fork-Join 异构并发组与 `for_each` 输出通道执行写集合互斥校验。

---

## 一、 Fork-Join 并发组写集合互斥公理

对于任意三元并发边 `src -> [W_1, W_2, ..., W_k] -> join_target`：

* 记每个并发成员节点 $W_i$ 向所属外层图 `variables` 提交的顶层目标变量集合为 $OutVars(W_i)$：
  * 当 $W_i$ 未挂载 `for_each` 时，$OutVars(W_i)$ 为其 `output`（或 `with.output` 映射后）的顶层变量名集合；
  * 当 $W_i$ 挂载了 `for_each` 时，$OutVars(W_i)$ 为其 `for_each.filter`、`for_each.collect` 与 `for_each.state` 的全部外层目标变量名集合。
* **两两互斥断言（Pairwise Disjointness）**：
  $$\forall i, j \in \{1, \dots, k\}, \quad i \neq j \implies OutVars(W_i) \cap OutVars(W_j) = \emptyset$$
  若存在任意变量 $v \in OutVars(W_i) \cap OutVars(W_j)$，编译期报错。

---

## 二、 `for_each` 内部三通道目标变量单射公理

对于任意挂载了 `for_each` 的节点 $M$：

1. **三通道间目标变量互斥**：
   * 令 $V_{\text{filter}} = \text{values}(M.\text{for\_each.filter})$；
   * 令 $V_{\text{collect}} = \text{target\_vars}(M.\text{for\_each.collect})$；
   * 令 $V_{\text{state}} = \text{set}(M.\text{for\_each.state})$。
   * 必须满足：
     * `filter` 内部不同分支映射的目标变量名两两互异；
     * `collect` 内部不同单次字段映射的目标变量名两两互异；
     * 三通道目标变量集合两两无交集：
       $$V_{\text{filter}} \cap V_{\text{collect}} = \emptyset, \quad V_{\text{filter}} \cap V_{\text{state}} = \emptyset, \quad V_{\text{collect}} \cap V_{\text{state}} = \emptyset$$
2. **`state` 并发度排他约束**：
   * 若 $V_{\text{state}} \neq \emptyset$，则必须满足 $M.\text{for\_each.concurrency} = 1$；若显式声明 `concurrency > 1`，编译期报错。
