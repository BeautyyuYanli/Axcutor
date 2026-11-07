# 02. Fork-Join 与 `for_each` 并发调度状态机

本规范定义异构结构化并发组（`-> [W_1, ..., W_k] ->`）与同构集合遍历（`for_each`）在运行时的快照锁定、确定性保序汇聚与屏障提交语义。

---

## 一、 Fork-Join 异构并发组调度状态机

当控制流沿三元并发边 `src -> [W_1, ..., W_k] -> join_target` 进入并发组时：

1. **Fork 快照锁定（Snapshot Lock）**：
   * 冻结当前图栈帧变量表的只读快照 $\rho_{\text{fork}} = \text{Snapshot}(\rho)$。
2. **隔离缓冲并发执行**：
   * 为每个并发成员 $W_i$（$i \in \{1, \dots, k\}$）分配独立的输出暂存槽 $\Delta_i = \emptyset$；
   * 并发启动所有 $W_i$，每个 $W_i$ 均以只读快照 $\rho_{\text{fork}}$ 作为其外层读取上下文（消除兄弟分支间的 Read-Write Race）；
   * 当 $W_i$ 正常完成时，将其产出的外层目标变量集合写入其专属暂存槽 $\Delta_i$（由静态分析保证 $\text{dom}(\Delta_i) \cap \text{dom}(\Delta_j) = \emptyset$）。
3. **Join 屏障原子提交（Barrier Commit）**：
   * 当且仅当全部 $k$ 个分支均正常完成到达 Join 屏障时，将所有暂存槽一次性原子合并回主图变量表：
     $$\rho \leftarrow \rho \triangleleft \left(\biguplus_{i=1}^{k} \Delta_i\right)$$
   * 随后控制令牌移交 `join_target`。

---

## 二、 `for_each` 同构集合遍历与保序汇聚状态机

设挂载了 `for_each` 的节点为 $M$，源集合求值结果为有序序列 $S = [(k_0, v_0), (k_1, v_1), \dots, (k_{n-1}, v_{n-1})]$（当源为 `Array` 时 $k_i = i$；当源为 `OrderedMap` 时按插入顺序排列，$k_i$ 为字符串键），长度为 $n = |S|$，外层目标输出变量集合为 $OutVars(M)$：

1. **空集合边界（$n = 0$）**：
   * 若 $n = 0$，不激活任何子迭代（计 `0` 步）；
   * 对每个外层目标变量 $v \in OutVars(M)$，直接初始化为空集合（当源为 `Array` 时写入 `[]`，当源为 `OrderedMap` 时写入 `{}`）；
   * 控制令牌直接沿默认出边 `M -> target` 前进。
2. **并发窗口控制与按序槽位分配（$n \ge 1$）**：
   * 运行时分配长度为 $n$ 的有序结果槽数组 $\text{Slots}[0 \dots n-1]$，并通过信号量控制同时处于激活状态的迭代数不超过 `concurrency`。
   * **单次迭代完成记录**：
     * 若第 $i$ 次迭代**正常完成**（返回正常分支 $b_i$ 或默认无分支出口）：提取该分支契约承诺产出的字段子集（若 $M$ 为 `CallNode`，则将产出的字段按 $M.\text{with.output}$ 重映射为外层目标变量名，忽略未在 `with.output` 中绑定的字段），得到有效产出映射 $\text{out}_i$ 并记录至 $\text{Slots}[i]$；
     * 若第 $i$ 次迭代触发可恢复异常并**被 `M.catch` 拦截**（进入异常分支 $c_i \in \text{dom}(M.\text{catch})$）：当次迭代的脏修改全部回滚，将空产出 $\text{out}_i = \emptyset$ 记录至 $\text{Slots}[i]$。
3. **屏障保序汇聚（Order-Preserving Aggregation）**：
   * 待全部 $n$ 个元素迭代完成后，对每个外层目标变量 $v \in OutVars(M)$，按原始下标顺序 $i = 0, 1, \dots, n-1$ 扫描 $\text{Slots}[i]$（即使 `concurrency > 1` 时各元素完成先后顺序不同，最终汇聚结果仍保持源集合顺序 $0 \dots n-1$）：
     * 依次提取所有满足 $v \in \text{dom}(\text{out}_i)$ 的产出值 $\text{out}_i[v]$：
       * 当源集合为 `Array` 时，按序追加至新列表 `result_list`；
       * 当源集合为 `OrderedMap` 时，按序以原键配对插入新有序映射 `result_map[k_i] = out_i[v]`；
     * 对于不满足 $v \in \text{dom}(\text{out}_i)$ 的槽位（即第 $i$ 次迭代走了未产出 $v$ 的正常业务分支，或被 `M.catch` 捕获），直接跳过。
   * 一次性将全部 $v \in OutVars(M)$ 的汇聚集合原子提交回外层图的 $\rho_{\text{outer}}$。
