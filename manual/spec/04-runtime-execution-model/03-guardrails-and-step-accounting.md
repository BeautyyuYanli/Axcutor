# 03. 运行护栏精确计量规则（Guardrails & Step Accounting）

本规范定义三大运行期护栏的形式化计量与触发规则：单图步数计数器（`steps`）与超限在途并发协同取消协议、调用树深度追踪（`call_depth`），以及节点级单次尝试超时计时器（`timeout_ms`）。

---

## 一、 单图步数计数器与超限协同取消（`max_steps` Accounting & Cooperative Cancellation）

每个激活的图栈帧 $F_G$（含根图、`use` 调用的子工作流图、每次激活的内联 `graph` 子图）维护一个局部变量 $\text{steps}(F_G)$，初始值为 $0$，上限为 $G.\text{max\_steps}$（未显式声明时默认值为 $1000$）。

1. **增量计步公式**：
   * **激活未挂载 `for_each` 的单个节点 $N$（含 `run`、内联 `graph`、`use: workflow:<pkg_path>` 以及 `use: builtin:<op>` 内建算子节点）**：
     在启动 $N$ 之前，执行 $\text{steps}(F_G) \leftarrow \text{steps}(F_G) + 1$。
     *(注：每次激活 `use: builtin:<op>` 节点同样计 $1$ 步，确保由纯 `builtin:*` 节点构成的循环同样受 `max_steps` 护栏保护，其运行期求值语义详见 [`05-builtin-operational-semantics.md`](./05-builtin-operational-semantics.md)；节点 $N$ 内部的 `retry` 重试不额外增加所属图 $F_G$ 的 $\text{steps}$；若 $N$ 是内联 `graph` 或 `use` 节点，$N$ 在父图 $F_G$ 中仅计 $1$ 步，而 $N$ 内部展开的子图栈帧 $F_{\text{sub}}$ 独立从 $0$ 开始计量其自身的 $\text{steps}(F_{\text{sub}})$。)*
   * **激活 Fork-Join 并发组 `[W_1, ..., W_k]`**：
     对组内每个未挂载 `for_each` 的成员节点计 $1$ 步（挂载了 `for_each` 的成员节点按其元素数计步）。
   * **激活挂载了 `for_each` 的节点 $M$（源集合长度为 $n = |S|$）**：
     每启动第 $i$ 个元素（$0 \le i < n$）的迭代前，执行 $\text{steps}(F_G) \leftarrow \text{steps}(F_G) + 1$（共累加 $n$ 步；当 $n = 0$ 时累加 $0$ 步）。
2. **超限触发判定与在途并发协同取消协议**：
   * 每当图栈帧 $F_G$（包括并发 `for_each` 准备激活下一个新元素迭代，或并发组分支推进时）检测到 $\text{steps}(F_G) > G.\text{max\_steps}$ 时：
     1. **停止派发与广播取消**：立即拒绝激活新节点或新元素迭代，并向当前图栈帧 $F_G$ 树下所有尚未完成的在途并发任务（包括 Fork-Join 并发组成员、`for_each` 在途并发迭代及其派生的子栈帧）广播不可捕获的 **`CancelledError`**（触发底层执行器 `abort_signal`）；
     2. **等待收敛与销毁栈帧**：等待 $F_G$ 下全部在途并发任务退出后，销毁并回滚图栈帧 $F_G$（丢弃一切未提交的局部状态与缓冲槽位）；
     3. **向上抛出异常**：
        * 若 $F_G$ 为子图栈帧（内联 `graph` 或 `use` 子工作流），向父节点边界抛出可恢复异常 **`max_steps`**；
        * 若 $F_G$ 为最顶层根图栈帧，立即抛出不可恢复的引擎级 **`MaxStepsExceededError`** 终止整个工作流。

---

## 二、 调用树深度追踪（`max_call_depth` Accounting）

运行时为每一条执行路径维护调用栈深度 $d$（上限为引擎配置参数 $\text{MAX\_CALL\_DEPTH}$，默认 $64$）：

* 根工作流启动时：$d = 1$；
* 沿当前执行路径每进入一层 `FunctionFrame`（`use: workflow:<pkg_path>`）或 `BlockFrame`（内联 `graph`），子栈帧的深度为：
  $$d_{\text{child}} = d_{\text{parent}} + 1$$
* 当 $d_{\text{child}} > \text{MAX\_CALL\_DEPTH}$ 时，立即抛出不可捕获的引擎级致命终止 **`StackOverflowError`**。
* **并发分叉独立性**：当在深度为 $d$ 的父帧中分叉启动并发组 `[W_1, ..., W_k]` 或并发 `for_each` 的多个迭代时，每个并发子分支均以 $d$ 为基数各自独立计算 $d + 1$，兄弟分支之间不横向累加。

---

## 三、 单次尝试计时器（Per-Attempt `timeout_ms` Timer）

当节点 $N$ 声明了 `timeout_ms: T`（$T > 0$）时：

* 计时器绑定在节点 $N$（或 `for_each` 下的单次元素迭代）的**每一次独立尝试（Attempt $a \in \{1, \dots, \text{max\_attempts}\}$）**上；
* 在第 $a$ 次尝试启动瞬间开启单调时钟计时器（预算为 $T$ 毫秒）；若在 $T$ 毫秒内该次尝试未完成，立即取消该次尝试（若为子图则级联取消子图内部所有在途任务），丢弃该次尝试的局部状态，并产生 **`timeout`** 异常交由该节点的 `retry` / `catch` 状态机处理。
