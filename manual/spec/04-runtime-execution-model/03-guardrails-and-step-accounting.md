# 03. 运行护栏精确计量规则（Guardrails & Step Accounting）

本规范定义运行期护栏、单次尝试超时、心跳配置与运行选项（`options`）的形式化计量、派生与触发规则：单图步数计数器（`steps`）与超限在途并发协同取消协议、调用树深度追踪（`call_depth`）、节点级单次尝试计时器（`timeout_ms`），以及执行请求运行选项与 Activity 心跳节流边界。

---

## 一、 单图步数计数器与超限协同取消（`max_steps` Accounting & Cooperative Cancellation）

每个激活的图栈帧 $F_G$（含根图、`use` 调用的子工作流图、每次激活的内联 `graph` 子图）维护一个局部变量 $\text{steps}(F_G)$，初始值为 $0$，上限为 $G.\text{max\_steps}$（未显式声明时默认值为 $1000$）。

1. **增量计步公式**：
   * **激活未挂载 `for_each` 的单个节点 $N$（含 `run`、内联 `graph`、`use: workflow:<PackagePath>` 以及 `use: builtin:<op>` 内建算子节点）**：
     在启动 $N$ 之前，执行 $\text{steps}(F_G) \leftarrow \text{steps}(F_G) + 1$。
     *(注：每次激活 `use: builtin:<op>` 节点同样计 $1$ 步，确保由纯 `builtin:*` 节点构成的循环同样受 `max_steps` 护栏保护，其运行期求值语义详见 [`05-builtin-operational-semantics.md`](./05-builtin-operational-semantics.md)；节点 $N$ 内部的 `retry` 重试不额外增加所属图 $F_G$ 的 $\text{steps}$；若 $N$ 是内联 `graph` 或 `use` 节点，$N$ 在父图 $F_G$ 中仅计 $1$ 步，而 $N$ 内部展开的子图栈帧 $F_{\text{sub}}$ 独立从 $0$ 开始计量其自身的 $\text{steps}(F_{\text{sub}})$。)*
   * **激活 Fork-Join 并发组 `[W_1, ..., W_k]`**：
     对组内每个未挂载 `for_each` 的成员节点计 $1$ 步（挂载了 `for_each` 的成员节点按其元素数计步）。
   * **激活挂载了 `for_each` 的节点 $M$（源集合长度为 $n = |S|$）**：
     每当第 $i$ 个元素（$0 \le i < n$）取得 `M.for_each.concurrency` 并发信号量许可并准备启动迭代前，执行 $\text{steps}(F_G) \leftarrow \text{steps}(F_G) + 1$（共累加 $n$ 步；当 $n = 0$ 时累加 $0$ 步）。
2. **超限触发判定与在途并发协同取消协议**：
   * 每当图栈帧 $F_G$（包括并发 `for_each` 准备激活下一个新元素迭代，或并发组分支推进时）检测到 $\text{steps}(F_G) > G.\text{max\_steps}$ 时：
     1. **停止派发与广播取消**：立即拒绝激活新节点或新元素迭代，并向当前图栈帧 $F_G$ 树下所有尚未完成的在途并发任务（包括 Fork-Join 并发组成员、`for_each` 在途并发迭代及其派生的子栈帧）广播不可捕获的 **`CancelledError`**（触发底层执行器 `abort_signal`）；
     2. **等待收敛与销毁栈帧**：等待 $F_G$ 下全部在途并发任务退出后，销毁并回滚图栈帧 $F_G$（丢弃一切未提交的局部状态与缓冲槽位）；
     3. **向上抛出异常**：
        * 若 $F_G$ 为子图栈帧（内联 `graph` 或 `use` 子工作流），向父节点边界抛出可恢复异常 **`max_steps`**；
        * 若 $F_G$ 为最顶层根图栈帧，立即抛出不可恢复的引擎级 **`MaxStepsExceededError`** 终止整个工作流。

---

## 二、 调用树深度追踪与跨子工作流传递（`max_call_depth` Accounting）

运行时为每一条执行路径维护逻辑调用栈深度 $d$（上限为运行选项 `max_call_depth`，默认 $64$）：

* **深度增减与帧类型区分**：
  * 根工作流启动时：$d = 1$；
  * 沿当前执行路径每进入一层 `FunctionFrame`（`use: workflow:<PackagePath>`）或 `BlockFrame`（内联 `graph`），子栈帧的深度为：
    $$d_{\text{child}} = d_{\text{parent}} + 1$$
  * `for_each` 单次元素迭代创建的局部诊断帧 `IterationFrame` 沿用父图栈帧的调用深度（$d_{\text{iter}} = d_{\text{parent}}$），不加深逻辑调用栈。
* **跨后端 Child Workflow 连续累计**：
  * 当 `use: workflow:<PackagePath>` 在调用边界因后端历史事件数提示或 `child_workflow_history_events` 阈值切换为后端 Child Workflow 实例时，父工作流将当前计算的逻辑深度 $d_{\text{child}}$ 随子请求传递，**不因切换后端执行实例而重置调用深度**（且切换为 Child Workflow 仅隔离后续事件历史，不截断父工作流已有历史）。
* **超限行为与并发分叉独立性**：
  * 当 $d_{\text{child}} > \text{max\_call\_depth}$ 时，立即抛出不可捕获的引擎级致命终止 **`StackOverflowError`**；
  * 当在深度为 $d$ 的父帧中分叉启动并发组 `[W_1, ..., W_k]` 或并发 `for_each` 的多个迭代时，每个并发子分支均以 $d$ 为基数各自独立计算，兄弟分支之间不横向累加。

---

## 三、 单次尝试计时器与排队、重试、清理边界（Per-Attempt `timeout_ms` Timer）

当业务节点 $N$（`run`、内联 `graph`、`use: workflow:<PackagePath>`）声明了 `timeout_ms` 或采用 Activity 默认超时预算时，遵循以下期限与生命周期契约：

1. **有效尝试预算派生与节点形态区分**：
   * **Activity 执行器节点**（`run.executor != "signal"`）：单次尝试的有效预算 $A$ 取节点显式声明的 `timeout_ms`，未声明时取运行选项 `activity_timeout_ms`（默认 $604800000\text{ ms}$，即 $7$ 天）。后端的 `start_to_close_timeout` 与 Activity 进程内的本地单调时钟 `deadline` 均从同一有效预算 $A$ 派生；**Activity 在任务队列中的排队等待时间不消耗预算 $A$**。
   * **复合节点**（内联 `graph` 与 `use: workflow:<PackagePath>`）：声明 `timeout_ms: T` 时，由工作流定时器约束该节点单次尝试的整体执行窗口（覆盖其内部后代节点的任务排队、执行尝试、重试退避与信号等待）；未声明 `timeout_ms` 时不设整体期限。外层复合节点的期限不会自动改写其内部 Activity 后代的本地 `deadline` 或心跳间隔。
   * **协作式信号等待节点**（`run.executor == "signal"`）：使用工作流计时器，每次重试重置等待期限；省略 `timeout_ms` 时无限期等待，不受 `activity_timeout_ms` 约束，也不占用 Activity Worker 槽位。若外部信号已在同一调度轮次内认领等待槽位，该认领结果优先于同轮次到期的尝试定时器；尚未认领时，超时清理槽位并按 `timeout` 重试或捕获，详见 [`04-signal-cooperative-executor.md`](../05-executor-abi/04-signal-cooperative-executor.md)。
2. **重试重置与退避独立性**：
   * 计时器绑定在节点 $N$（或 `for_each` 下的单次元素迭代）的**每一次独立尝试（Attempt $a \in \{1, \dots, \text{max\_attempts}\}$）**上；
   * 每次进入重试时重置尝试期限，两次尝试之间的 `retry.backoff_ms` 退避等待不计入下一次尝试的超时预算。
   * 对于 Activity 节点声明的 `retry`，仅当 `retry.on` 包含 `"timeout"`、不包含 `"contract"`、$\text{backoff\_ms} > 0$ 且参数处于后端原生重试可表示范围内时，运行时可下推为固定退避系数 $1.0$ 的后端原生重试；其余情形（含默认 $\text{backoff\_ms} = 0$ 的立即重试或包含需工作流二次核验的 `"contract"`）均由工作流状态机循环驱动，二者对外保持完全一致的总尝试预算语义。
3. **超时取消后的任务排空（Drain on Timeout）**：
   * 当单次尝试触及超时边界时，运行时立即向该次尝试（或复合节点内部所有在途后代任务）发起取消，并**等待在途任务收到取消并完成清理排空**后，才丢弃该次尝试的局部状态并产生 **`timeout`** 异常进入 `retry` / `catch` 状态机；因此 `timeout_ms` 是发起取消的期限，而非下游 `catch` 或下一次重试启动时刻的硬实时上界。

---

## 四、 执行请求运行选项与 Activity 心跳节流规范（Runtime Options & Heartbeat）

运行时接收可选的 `options` 映射（由宿主 `ExecutionService(..., options=...)` 或直接工作流请求传入），并在工作流执行与重放入口通过 `parse_runtime_options` 统一校验与补齐：

### 1. 六项运行选项 schema、默认值与心跳派生不变量

| 选项键 | 默认值 | 值域与关联约束 |
| :--- | :--- | :--- |
| **`activity_timeout_ms`** | `604800000`（$7$ 天） | 正整数（$\mathbb{Z}_{\ge 1}$，排除布尔值）；Activity 节点未显式声明 `timeout_ms` 时的单次尝试预算。 |
| **`heartbeat_timeout_ms`**（记为 $T$） | `30000`（$30\text{ s}$） | 正整数（$\mathbb{Z}_{\ge 1}$）；请求的 Activity Worker 失活检测窗口，后端实际生效值为 $\min(T, A)$。 |
| **`heartbeat_interval_ms`**（记为 $I$） | **省略时派生为 $\max(1, \lfloor T / 3 \rfloor)$**（默认 `10000`） | 正整数（$\mathbb{Z}_{\ge 1}$），且最终必须满足严格不等式 **$I < T$**；表示 Activity 进程内调用心跳的本地间隔。 |
| **`max_call_depth`** | `64` | 正整数（$\mathbb{Z}_{\ge 1}$）；逻辑调用栈深度上限。 |
| **`child_workflow_history_events`** | `null` | `null` 或正整数（$\mathbb{Z}_{\ge 1}$）；在 `use: workflow:<PackagePath>` 边界触发切换为 Child Workflow 的历史事件数阈值提示。 |
| **`task_queues`** | `{}` | `Map<String, NonEmptyString>`；按 `run.executor` 名称将 Activity 路由至指定任务队列，未命中时沿用当前工作流队列。 |

* **心跳间隔派生与校验顺序**：
  1. 拒绝未知顶层键、非字典结构、布尔值，以及针对四个整数选项传入的显式 `null` 或非正整数；
  2. **仅当 `heartbeat_interval_ms` 键缺失（省略）时**，按已确定的 $T$ 自动计算派生值 $I = \max(1, \lfloor T / 3 \rfloor)$；若调用方显式提供了 `heartbeat_interval_ms`，则原样保留显式值（不随 $T$ 重算）；
  3. 最终校验 $I < T$（例如仅设置 $T = 1000$ 时派生 $I = 333$；$T = 2$ 时派生 $I = 1$；而 $T = 1$ 因派生 $I = 1 \not< 1$ 被拒绝）。
* **宿主原始覆盖值持久化与重放演进**：
  * 宿主 `ExecutionService` 在构造期即复用同一校验规则（非法时抛出 `ValueError`），但在提交根工作流请求时**仅持久化调用方传入的原始覆盖字典**，不预先固化默认值；
  * 工作流在执行与重放入口调用 `parse_runtime_options` 补齐缺失键的默认值（直接工作流请求的非法 `options` 转化为不可恢复的 **`EngineError`**），并将展开后的完整六键字典传递给派生的 Child Workflow（由于显式值在重复解析时原样保留，Child 不会因二次解析而改写父工作流已确定的心跳间隔）。

### 2. 应用心跳与 Worker 节流配对

* Activity 在尝试启动后立即发送首次心跳，随后每隔 $I$ 毫秒调用一次 `activity.heartbeat(...)`；后端通过心跳响应向在途 Activity 投递取消通知。
* Worker 通过 `create_worker(..., **kwargs)` 透传底层 SDK 资源与节流参数（默认沿用 SDK 的 `max_heartbeat_throttle_interval = 60s`、`default_heartbeat_throttle_interval = 30s`）。SDK 仅对应用实际发出的心跳做合并节流，不会在应用未调用心跳时自动生成心跳。
* 生产默认配置（$T = 30000\text{ ms}, I = 10000\text{ ms}$ 配合 SDK 默认节流）降低长耗时任务的 RPC 频率；需要快速取消响应的交互式宿主（如 Playground）需同时将应用心跳间隔 `options["heartbeat_interval_ms"]` 与 Worker 的 `max_heartbeat_throttle_interval` 成对缩短（例如同设为 `200ms`），而无需缩短失活检测窗口 $T$。
