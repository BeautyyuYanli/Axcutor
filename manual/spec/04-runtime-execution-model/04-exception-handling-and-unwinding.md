# 04. 三级异常体系、`retry` / `catch` / `fail` 状态机与栈展开

本规范定义运行时异常的分类、节点内原地重试与边界捕获状态机，以及未捕获异常的跨栈帧展开（Stack Unwinding）与协同取消协议。

---

## 一、 异常分类与捕获权限表

| 异常类别 | 标识符 / 异常名 | 产生源 | 是否允许被 `retry` / `catch` 拦截 |
| :--- | :--- | :--- | :--- |
| **可恢复运行时异常** | `execution` | `run` 执行器运行故障，或子图主动走向 `-> fail` | **允许** |
| **可恢复运行时异常** | `timeout` | 单次尝试耗时超出节点 `timeout_ms` | **允许** |
| **可恢复运行时异常** | `contract` | 节点返回的 `output` 违反 `TypeSpec` 或返回非法 `branch` | **允许** |
| **可恢复运行时异常** | `max_steps` | 内联 `graph` 或 `use` 子工作流超出其 `graph.max_steps` | **允许**（仅在包裹该子图的父节点边界） |
| **不可恢复引擎 Panic** | `StackOverflowError` | 调用栈深度 $d > \text{MAX\_CALL\_DEPTH}$ | **禁止拦截**（直接穿透终止） |
| **不可恢复引擎 Panic** | `MaxStepsExceededError` | 根工作流（Root Graph）自身超出 `max_steps` | **禁止拦截**（直接穿透终止） |
| **不可恢复引擎 Panic** | `CancelledError` | 外部宿主取消信号，或并发兄弟分支 Fail-Fast 协同取消 | **禁止拦截**（直接穿透终止） |

---

## 二、 节点级执行、重试与捕获状态机（Node Execution State Machine）

对于任意非内建节点 $N$（若挂载了 `for_each`，则针对单次元素迭代独立运行本状态机）：

```mermaid
stateDiagram-v2
    [*] --> LockSnapshot: 激活节点 (提取只读快照 S_in, attempt = 1)
    LockSnapshot --> ExecuteAttempt: 启动第 attempt 次尝试 (开启 timeout_ms 计时)
    ExecuteAttempt --> ValidateContract: 执行完成返回 (branch, output)
    ValidateContract --> CommitNormal: Schema 与分支校验通过
    ValidateContract --> HandleException: 校验违约 (产生 E = contract)
    ExecuteAttempt --> HandleException: 抛出 execution / timeout / max_steps (记为 E)
    HandleException --> BackoffWait: E 属于 retry.on 且 attempt < max_attempts (回滚当次尝试脏状态)
    BackoffWait --> ExecuteAttempt: 等待 backoff_ms 后 attempt += 1 (重放同一份 S_in)
    HandleException --> MaterializeCatch: 不满足重试条件，但 E 匹配 catch.<c_branch> (回滚脏状态)
    HandleException --> UnwindToParent: 未匹配任何 catch 分支 (或 E 为不可恢复 Panic)
    CommitNormal --> [*]: 原子合并 output 子集，沿正常分支出边前进
    MaterializeCatch --> [*]: 不写入任何 output (output: [])，沿 N.<c_branch> 出边前进
    UnwindToParent --> [*]: 触发所属图栈帧 Unwind 展开
```

### `catch` 分支匹配规则

* 节点的 `catch` 声明中，各分支的显式选择器集合两两互斥，且最多有一个分支声明通配符 `"*"`（代表差集 $\{ \texttt{execution}, \texttt{timeout}, \texttt{contract}, \texttt{max\_steps} \} \setminus \bigcup \text{ExplicitSelectors}$）。
* 当可恢复异常 $E$ 从重试控制器逃逸时，若存在分支 $c \in \text{dom}(N.\text{catch})$ 使得 $E \in \text{Selectors}(c)$，则异常被消费，控制令牌以空输出（$\Delta = \emptyset$）转入异常出边 $N.c$。

---

## 三、 `-> fail` 终态与未捕获异常栈展开（Stack Unwinding & Fail-Fast Cancellation）

### 1. 主动到达 `-> fail` 终态
当图 $G$ 内的控制令牌沿边 `src -> fail` 到达 `fail` 终态时：
1. 立即停止图 $G$ 的调度，销毁图栈帧 $F_G$ 并丢弃其全部局部变量修改；
2. 构造一个可恢复异常 $E = \texttt{execution}$，并将其抛向包裹图 $G$ 的父节点边界（若 $G$ 为根图，则整个工作流以未捕获 `execution` 异常失败终止）。

### 2. 未捕获异常的协同取消与跨帧冒泡
当图 $G$ 内的节点（或并发分支 / `for_each` 迭代）抛出未被局部 `catch` 拦截的异常 $E$ 时：
1. **同组并发兄弟 Fail-Fast 协同取消**：
   * 若出错点位于 Fork-Join 并发组 `[W_1, ..., W_k]` 或并发 `for_each` 内部，协调器立即向同组所有尚未完成的并发任务发送不可捕获的 `CancelledError` 并等待其退出，丢弃该并发组或 `for_each` 已暂存的全部缓冲槽位。
2. **当前图栈帧回滚中断**：
   * 销毁当前图栈帧 $F_G$，不向父栈帧提交任何输出。
3. **向父栈帧传播**：
   * 将异常 $E$（保留其原始选择器类型 `execution` / `timeout` / `contract` / `max_steps`）传递至父栈帧中调用该子图的 `InlineGraphNode` 或 `CallNode` 边界，进入父节点的 `retry` / `catch` 状态机处理。
