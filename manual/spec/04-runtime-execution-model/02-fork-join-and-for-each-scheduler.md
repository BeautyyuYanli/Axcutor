# 02. Fork-Join 与 `for_each` 并发调度状态机

本规范定义异构结构化并发组 `-> [W_1, ..., W_k] ->` 与同构集合遍历 `for_each` 在运行时的快照锁定、隔离执行、确定性保序汇聚、屏障提交以及 Fail-Fast 异常中止语义。

---

## 一、 Fork-Join 异构并发组调度状态机

当控制流沿三元并发边 `src -> [W_1, ..., W_k] -> join_target` 进入并发组时，调度器按以下状态转移执行：

1. **Fork 快照锁定（Snapshot Lock）**：
   * 冻结当前图栈帧变量表 $\rho$ 的只读快照：
     $$\rho_{\text{fork}} = \text{Snapshot}(\rho)$$
2. **隔离缓冲并发执行（Isolated Concurrent Execution）**：
   * 对每个并发成员 $W_i$，$i \in \{1, \dots, k\}$，分配独立的输出暂存槽 $\Delta_i = \emptyset$；
   * 并发激活所有 $W_i$，每个分支均以只读快照 $\rho_{\text{fork}}$ 作为外层变量读取上下文，从而在运行时消除兄弟分支间的读写数据竞争；
   * 当分支 $W_i$ 正常完成时，将其产出的外层目标变量集合写入专属暂存槽 $\Delta_i$。静态单写者检查保证任意两分支的写入域两两不相交，即对任意 $i \neq j$ 均满足 $\text{dom}(\Delta_i) \cap \text{dom}(\Delta_j) = \emptyset$。
3. **Join 屏障原子提交（Barrier Commit）**：
   * 当全部 $k$ 个分支均正常完成并到达 Join 屏障时，将所有暂存槽一次性原子合并回当前图栈帧变量表：
     $$\rho \leftarrow \rho \triangleleft \left(\biguplus_{i=1}^{k} \Delta_i\right)$$
   * 合并完成后，控制令牌移交 `join_target`。
4. **Fail-Fast 屏障中止转移（Barrier Abort on Uncaught Exception）**：
   * 若任一并发分支 $W_i$ 抛出未被其内部 `catch` 拦截的异常 $E$，Join 屏障立即中止等待并触发 Fail-Fast 协同取消：向其余所有尚在运行的兄弟分支 $W_j$，$j \neq i$，广播不可捕获的 `CancelledError`，等待全部在途分支退出后丢弃所有暂存槽 $\Delta_1, \dots, \Delta_k$，保持主图变量表 $\rho$ 不变，并将异常 $E$ 移交当前图栈帧的异常展开流程处理，详见 [`04-exception-handling-and-unwinding.md`](./04-exception-handling-and-unwinding.md)。

---

## 二、 `for_each` 同构集合遍历与保序汇聚状态机

设挂载了 `for_each` 的节点为 $M$，外层目标输出变量集合为 $\text{OutVars}(M)$。源集合表达式求值结果为长度为 $n = |S|$ 的有序序列：
$$S = [(k_0, v_0), (k_1, v_1), \dots, (k_{n-1}, v_{n-1})]$$
其中，当源集合为 `Array` 时，$k_i = i$；当源集合为 `OrderedMap` 时，序列按插入顺序排列且 $k_i$ 为对应的字符串键。调度器按以下状态转移执行：

1. **空集合边界转移（$n = 0$）**：
   * 当 $n = 0$ 时，不激活任何子迭代，执行步数计为 `0`；
   * 对每个外层目标变量 $v \in \text{OutVars}(M)$，按源集合类型直接初始化为空集合：若源集合为 `Array` 则写入 `[]`，若源集合为 `OrderedMap` 则写入 `{}`；
   * 控制令牌直接沿默认出边 `M -> target` 前进。
2. **并发窗口控制与单次迭代槽位记录（$n \ge 1$）**：
   * 分配长度为 $n$ 的有序结果槽数组 $\text{Slots}[0 \dots n-1]$，并通过信号量限制同时处于激活状态的迭代数不超过 `M.for_each.concurrency`。
   * 对每个下标 $i \in \{0, \dots, n-1\}$，第 $i$ 次迭代独立运行节点级执行、重试与捕获状态机，其终止结果按以下三类转移处理：
     * **正常完成**：当迭代返回正常分支 $b_i$ 或默认无分支出口时，提取该分支契约承诺产出的字段子集；若 $M$ 为 `CallNode`，则按 $M.\text{with.output}$ 将子图产出字段重映射为外层目标变量名并忽略未绑定的字段，最终形成有效产出映射 $\text{out}_i$ 并写入 $\text{Slots}[i] \leftarrow \text{out}_i$；
     * **局部 `catch` 拦截**：当迭代触发可恢复异常且匹配异常分支 $c_i \in \text{dom}(M.\text{catch})$ 时，回滚当次迭代的局部状态并将空产出写入槽位 $\text{Slots}[i] \leftarrow \emptyset$，其余元素迭代继续正常调度；
     * **未捕获异常与 Fail-Fast 屏障中止**：当迭代因未配置 `M.catch`、异常类型未匹配任何 `catch` 分支或触发不可恢复 Panic 而抛出未被 $M.\text{catch}$ 拦截的异常 $E$ 时，`for_each` 屏障立即中止：向所有尚在执行的在途元素迭代广播不可捕获的 `CancelledError` 并停止派生后续未启动的迭代，等待在途迭代全部退出后丢弃整个结果槽数组 $\text{Slots}[0 \dots n-1]$，不对外层变量表 $\rho_{\text{outer}}$ 进行任何写入，并将异常 $E$ 向上传播至当前图栈帧的异常展开流程，详见 [`04-exception-handling-and-unwinding.md`](./04-exception-handling-and-unwinding.md)。
3. **屏障保序汇聚与原子提交（Order-Preserving Aggregation）**：
   * 当全部 $n$ 个元素迭代均正常完成或经 `M.catch` 拦截收敛后，对每个外层目标变量 $v \in \text{OutVars}(M)$，严格按源集合下标顺序 $i = 0, 1, \dots, n-1$ 扫描 $\text{Slots}[i]$，保证最终汇聚顺序与 `concurrency` 并发完成先后次序无关：
     * 若 $v \in \text{dom}(\text{out}_i)$，则提取产出值 $\text{out}_i[v]$：当源集合为 `Array` 时按序追加至目标列表 `result_list`；当源集合为 `OrderedMap` 时按序以原键 $k_i$ 插入目标有序映射 `result_map[k_i] = out_i[v]`；
     * 若 $v \notin \text{dom}(\text{out}_i)$，即第 $i$ 次迭代走向未产出 $v$ 的正常分支或已由 `M.catch` 拦截置空，则直接跳过该槽位。
   * 全部目标变量汇聚完毕后，一次性将所有 $v \in \text{OutVars}(M)$ 的聚合结果原子写入外层图栈帧变量表 $\rho_{\text{outer}}$。
