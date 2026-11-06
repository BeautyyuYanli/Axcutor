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

## 二、 `for_each` 同构集合遍历与三通道保序汇聚状态机

设源集合求值结果为有序序列 $S = [(k_0, v_0), (k_1, v_1), \dots, (k_{n-1}, v_{n-1})]$（当源为 `Array` 时 $k_i = i$；当源为 `OrderedMap` 时按插入顺序排列，$k_i$ 为字符串键），长度为 $n = |S|$：

1. **空集合边界（$n = 0$）**：
   * 若 $n = 0$，不激活任何子迭代（计 `0` 步）：
     * 所有 `filter` 目标变量与 `collect` 目标变量直接初始化为空集合（源为列表时写入 `[]`，源为有序映射或 `flatten: true` 时分别写入 `{}` 或 `[]`）；
     * 所有 `state` 目标变量保持进入该节点前的初始值不变；
     * 控制令牌直接沿默认出边前进。
2. **并发窗口控制与按序槽位分配（$n \ge 1$）**：
   * 运行时分配长度为 $n$ 的有序结果槽数组 $\text{Slots}[0 \dots n-1]$，并通过信号量控制同时处于激活状态的迭代数不超过 `concurrency`（当声明了 `state` 时 `concurrency = 1`，按 $i = 0, 1, \dots, n-1$ 串行执行，并将第 $i-1$ 步更新后的 `state` 传入第 $i$ 步）。
   * 第 $i$ 次迭代完成（正常返回分支 $b_i$ 或进入 `catch` 异常分支 $c_i$）后，将其所选分支名 $\text{choice}_i$ 与有效产出 $\text{out}_i$ 记录到 $\text{Slots}[i]$。
3. **屏障保序汇聚（Order-Preserving Aggregation）**：
   * 待全部 $n$ 个元素迭代完成后，按原始下标顺序 $i = 0, 1, \dots, n-1$ 扫描 $\text{Slots}[i]$（即使 `concurrency > 1` 时各元素完成先后顺序不同，最终汇聚结果仍保持源集合顺序 $0 \dots n-1$）：
     * **`filter` 汇聚**：对每个配置 `filter.<branch>: <target_var>`，依次将满足 $\text{choice}_i = \text{branch}$ 的原元素 $v_i$（或键值对 $(k_i, v_i)$）追加到 `<target_var>`；
     * **`collect` 汇聚（`flatten: false`）**：对每个配置 `collect.<field>: <target_var>`，依次提取满足 $\text{field} \in \text{dom}(\text{out}_i)$ 的产出值 $\text{out}_i[\text{field}]$（若源为 `OrderedMap` 则配对原键 $(k_i, \text{out}_i[\text{field}])$）追加到 `<target_var>`；
     * **`collect` 汇聚（`flatten: true`）**：按 $i = 0 \dots n-1$ 顺序将满足 $\text{field} \in \text{dom}(\text{out}_i)$ 的子列表 $\text{out}_i[\text{field}]$ 首尾拼接（Concatenate）为一维列表写入 `<target_var>`；
     * **`state` 提交**：将第 $n-1$ 次迭代结束后的最终累积状态写入外层 `<state_var>`。
   * 一次性将全部三通道目标变量原子提交回外层图的 $\rho$。
