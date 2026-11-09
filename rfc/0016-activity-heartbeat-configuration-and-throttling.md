# RFC-0016: 运行时超时、配额护栏与 Activity 心跳节流统一收敛规范

* **状态**：Proposed
* **所属分类**：运行时配置收敛、Temporal Activity 生命周期管理与配额护栏规范（Runtime Configuration Convergence, Activity Heartbeat Throttling & Guardrail Specification）
* **前置关联提案**：
  * [`RFC-0011: Axcutor 编译器前后端与 Temporal 运行时参考实现架构`](./0011-compiler-and-temporal-runtime-architecture.md)
  * [`RFC-0012: 结构化 run 执行器分派机制、移除自然语言变量正则提取与 builtin 可信 Python 执行器规范`](./0012-run-executor-and-builtin-python.md)
  * [`RFC-0014: 基于预置 Python 模块引用的 module 执行器、__axcutor__.yaml 包同构布局与契约模板 SHA 校验规范`](./0014-module-python-executor.md)
  * [`RFC-0017: 协作式挂起执行器 signal、Temporal Update 恢复协议与宿主 :signal 接口规范`](./0017-cooperative-signal-executor.md)
* **核心解决的问题**：
  1. **确立全栈 `timeout`、`max_*`、并发与 `heartbeat` 的三层最小化配置矩阵**：将散落在编译器、Workflow 解释器、Activity Worker、宿主服务与容器编排中的超时和限额机制按职责收束为“DSL 业务语义层（4 项可选字段）”、“运行时选项与宿主默认参数层（生产零配置）”与“内部固定安全常量层”，在不损失正确性的前提下杜绝过度工程化与冗余参数配对；
  2. **消除生产环境 Worker 硬编码 `100ms` 心跳节流上限导致的高频 RPC 写放大问题**：移除 `worker.py` 中 `create_worker` 硬编码的 `max_heartbeat_throttle_interval=timedelta(milliseconds=100)` 与 `default_heartbeat_throttle_interval=timedelta(milliseconds=100)`，恢复 Temporal SDK 原生的客户端节流机制（当设置 `heartbeat_timeout` 时为 $\min(\text{effective\_heartbeat\_timeout} \times 0.8,\ \text{max\_heartbeat\_throttle\_interval})$），将交互式工作台与测试套件的快速取消送达需求通过显式应用层唤醒间隔与 `max_heartbeat_throttle_interval` 参数按场景隔离；
  3. **确立符合 Temporal 最佳实践的生产默认心跳参数与无截断线性自推导规则**：将 `workflow.py` 中 `AxcutorInterpreterWorkflow._parse_options` 的生产默认心跳参数由 `heartbeat_interval_ms: 1000, heartbeat_timeout_ms: 10000` 提升为 `heartbeat_interval_ms: 10000, heartbeat_timeout_ms: 30000`；当调用方仅指定 `heartbeat_timeout_ms`（整数 $\ge 2$）时自动按无截断线性比例 $\max(1,\ \lfloor\text{heartbeat\_timeout\_ms} / 3\rfloor)$ 推导 `heartbeat_interval_ms`，消除双参数手动配对负担、单参数调小引发的 `interval >= timeout` 冲突以及无意义的协程空转唤醒；
  4. **厘清 Temporal Server 侧超时、SDK Core 本地计时器与 Worker 应用层看门狗（Dual Deadline Watchdog）的正交职责边界**：阐明为何 Activity 型 `run` 节点必须默认开启 `heartbeat_timeout`（防止 Worker 物理崩溃时悬挂在 7 天的 `activity_timeout_ms` 上，并驱动 SDK 按超时比例计算节流窗口），为何持续发送心跳是 `WAIT_CANCELLATION_COMPLETED` 取消信号送达的必要条件，以及为何 `RunActivities.execute_run` 必须同时保留本地 `deadline` 协程与 `expires_at` 单调时钟复查（确保在当前尝试内不经过 SDK 缓冲延迟即刻置位业务 `AbortSignal`、排空当前尝试拥有的本地协程任务并拦截超时后才完成契约校验的迟到成功结果）。

---

## 一、 问题背景与根因溯源

### 问题 16.1：测试环境的毫秒级取消响应需求污染了生产 Worker 与默认工作流配置

在 [`RFC-0011`](./0011-compiler-and-temporal-runtime-architecture.md) 与 [`RFC-0012`](./0012-run-executor-and-builtin-python.md) 的落地实现中，`axcutor_runtime` 在两处核心路径引入了极高频的心跳参数：

1. **`worker.py` 硬编码 `100ms` 节流上限**：
   在 `python/axcutor_runtime/src/axcutor_runtime/worker.py` 的 `create_worker` 中，无条件向 `temporalio.worker.Worker(...)` 传入了：
   ```python
   max_heartbeat_throttle_interval=timedelta(milliseconds=100),
   default_heartbeat_throttle_interval=timedelta(milliseconds=100),
   ```
   这不仅绕过了 Temporal SDK 默认 `default=30s, max=60s` 的客户端合并节流机制，还导致外部调用方无法通过 `create_worker(..., **kwargs)` 自定义这两个参数（会触发 Python `TypeError: multiple values for keyword argument`）。
2. **`workflow.py` 默认每秒唤醒一次心跳且强制要求双参数配对**：
   在 `python/axcutor_runtime/src/axcutor_runtime/workflow.py` 的 `AxcutorInterpreterWorkflow._parse_options` 中，全局默认参数被设为：
   ```python
   "heartbeat_interval_ms": 1000,
   "heartbeat_timeout_ms": 10000,
   ```
   且直接执行 `if result["heartbeat_interval_ms"] >= result["heartbeat_timeout_ms"]: raise ValueError(...)`。若调用方或测试仅传入 `options={"heartbeat_timeout_ms": 500}` 而未同时传入更小的 `heartbeat_interval_ms`，会直接因默认 `1000 >= 500` 校验失败。

**历史引入原因**：
在 Temporal 架构中，正在执行的 Activity 唯有通过向 Temporal Server 发送 `RecordActivityTaskHeartbeat` RPC 并读取响应中的 `cancel_requested` 标志，才能在未到达本地超时前感知到父工作流或并发分支的取消请求（`ActivityCancellationType.WAIT_CANCELLATION_COMPLETED`）。为了使集成测试套件（`tests/conftest.py`、`tests/host/test_service.py`、`tests/host/test_http.py` 中的 `heartbeat_interval_ms=20, heartbeat_timeout_ms=1000`）能够在百毫秒内完成取消收敛断言，实现者当时直接在生产函数 `create_worker` 中将 SDK 客户端节流器硬编码压低到了 `100ms`。

### 问题 16.2：绕过客户端节流的高频心跳违背 Temporal 设计规范并引发写放大

在生产环境的 Temporal 集群中，将 Worker 节流间隔硬编码为 `100ms` 并配合 `1000ms` 的循环心跳会产生严重性能隐患：
* **Mutable State 写放大与锁争用**：每一次实际发往 Temporal Server 的 `RecordActivityTaskHeartbeat` RPC 都要求 History Service 获取对应 Workflow Execution 的内存锁，并更新持久化存储中的 Mutable State 记录；
* **并发节点下的雪崩效应**：当工作流通过 `for_each` 或 `fork_join` 并发调度数十至数百个长耗时节点（如批处理、LLM 调用或外部轮询）时，每个活跃 Activity 每秒触发一次未节流的 Server RPC，会导致 Temporal History 分片承受大量无效写负载；
* **职责边界混淆**：Temporal 官方设计中，`activity.heartbeat()` 用于 **长耗时任务的存活探测（Liveness Detection）**、**取消信号搭载（Cancellation Delivery）** 与 **低频进度检查点（Progress Checkpointing）**；Axcutor 当前仅传递 `{"attempt": activity.info().attempt}` 而未携带业务检查点，绝不应将心跳当作毫秒级状态同步通道或高频预写日志（WAL）使用。

### 问题 16.3：全栈超时、限额与保活机制缺乏统一的收敛边界

除心跳参数外，当前实现在编译器（`axcutor-core`）、Workflow 解释器（`workflow.py`）、Activity 执行器（`activities.py`）、宿主服务（`service.py` / `cli.py`）与 Playground 中存在多类 `timeout`、`max_*` 与轮询间隔机制。若缺乏明确的分类准则，极易出现“将底层基础设施参数暴露到上层 DSL”或“需要用户同时调整多个耦合参数才能保持不变量”的过度工程化倾向。

---

## 二、 核心设计决策

### 决策 1：确立三层最小化配置矩阵（DSL 业务层 / 运行时与宿主可覆盖默认层 / 内部固定护栏层）

为确保**需要配置的部分尽量少、保持正确性且不发生过度工程化**，系统内所有超时、配额与心跳机制严格按所有权划分为三层，严禁跨层泄漏：

#### 第一层：DSL 语言级配置（唯一面向工作流编写者的配置面，共 4 项可选字段）

工作流作者仅需在 YAML/JSON 中声明与业务控制流语义直接相关的 4 个正交参数（未声明时均由编译器或运行时提供安全默认值，**严禁在 DSL 中引入任何心跳、队列或历史长度配置**）：

| DSL 字段路径 | 适用范围 | 默认值与取值约束 | 运行期语义与执行载体 |
| :--- | :--- | :--- | :--- |
| **`graph.max_steps`** | 根 `graph` 及内联 `graph` | 默认 `1000`（整数 $\ge 1$） | 每次激活非 `for_each` 节点（含 `builtin:*`）或 `for_each` 单个元素获取并发信号量后计 `1` 步（空集合 `for_each` 计 `0` 步，节点内 `retry` 不额外计步）。超限时先取消并排空当前图栈帧下全部在途并发任务；根图（`call_depth == 1`）抛不可恢复的 `MaxStepsExceededError`，子图（`call_depth > 1`）抛可恢复的 `max_steps` 异常。 |
| **`timeout_ms`** | 顶层 `run`、图内 `run` / `graph` / `use: workflow:*`（`builtin:*` 禁用） | 默认 `None`（整数 $\ge 1$） | 作用于**单次独立尝试（Per-Attempt）**。对复合节点（`graph` / `use: workflow:*`），缺省时不设限，显式声明时在 Workflow 内通过 `_with_timeout(..., signal=False)` 竞速 `workflow.sleep` 定时器；对 Activity 型 `run` 节点（`is_activity = body["kind"] == "run" and body["run"]["executor"] != "signal"`），缺省时回退至 `options.activity_timeout_ms`（7 天），同时作为 Temporal `start_to_close_timeout` 与 Activity 本地 `deadline` 预算；对 [`RFC-0017`](./0017-cooperative-signal-executor.md) 的 `signal` 节点（`is_signal`），缺省时无限期挂起（不受 `activity_timeout_ms` 约束），显式声明时由 Workflow `_with_timeout(..., signal=True)` 定时器驱动。 |
| **`retry`** | 同 `timeout_ms` | 默认 `None`（启用时 `max_attempts >= 2` 必填，`backoff_ms` 默认 `0`，Activity 型节点与复合节点 `on` 默认 `["execution", "timeout"]`，`RFC-0017` `signal` 节点 `on` 默认 `["timeout"]`） | 失败尝试回滚局部状态并重放同一份只读输入快照。当且仅当满足 6 项安全条件（`is_activity`、`retry is not None`、`"timeout" in on`、`"contract" not in on`、`0 < backoff_ms <= 2147483647000`、`max_attempts <= 2147483647`）时，自动下推至 Temporal Activity 原生 `RetryPolicy`（`backoff_coefficient=1.0`）；其余情形（含 `signal` 节点）自动由 Workflow 循环接管，**零人工配置开关**。 |
| **`for_each.concurrency`** | 挂载了 `for_each` 的节点 | 默认 `1`（整数 $\ge 1$） | 通过 `asyncio.Semaphore(concurrency)` 限制同一 `for_each` 节点内同时处于激活执行状态的元素迭代数；任一迭代发生未捕获异常时立即置位 `stopped` 停止激活后续迭代，并协同取消在途迭代。 |

#### 第二层：运行时选项与宿主进程级默认参数（面向宿主/测试注入，生产环境零配置）

第二层包含两类载体：**随持久化请求传入的重放确定性选项 `request["options"]`**（共 6 个键，生产环境下 `ExecutionService` 默认传 `{}`），以及**宿主/Worker 进程构造时的本地基础设施默认参数**（通过 Python 构造参数或 `create_worker(**kwargs)` 透传，不增加 CLI flag）：

1. **Workflow 持久化运行时选项 `request["options"]`（`AxcutorInterpreterWorkflow._parse_options`）**：

| `options` 键名 | 生产默认值 | 取值约束与职责说明 |
| :--- | :--- | :--- |
| **`heartbeat_timeout_ms`** | **`30000`**（30 秒） | 整数 $\ge 2$（且须满足 `1 <= heartbeat_interval_ms < heartbeat_timeout_ms`）。传给 `workflow.execute_activity(..., heartbeat_timeout=...)`，决定 Worker 物理崩溃后的故障检测窗口，并与有效 `start_to_close_timeout` 共同决定 SDK 客户端心跳节流周期（仅对 Activity 型 `run` 节点生效）。 |
| **`heartbeat_interval_ms`** | **`10000`**（10 秒，或由 `heartbeat_timeout_ms` 自动缩放） | 正整数 $\ge 1$ 且 `< heartbeat_timeout_ms`。传给 Activity 请求载荷，控制 `RunActivities.execute_run` 内部 `heartbeats()` 协程的本地睡眠唤醒间隔。**支持无截断单参数自推导**：当调用方仅传入 `heartbeat_timeout_ms` 时，自动按 $\max(1,\ \lfloor\text{heartbeat\_timeout\_ms} / 3\rfloor)$ 推导，无需手动配对。 |
| **`activity_timeout_ms`** | **`604800000`**（7 天） | 正整数 $\ge 1$。当 Activity 型 `run` 节点未声明 `timeout_ms` 时的单次尝试执行时长（`start_to_close_timeout`）兜底上限（Temporal 要求 Activity 至少指定 `start_to_close_timeout` 或 `schedule_to_close_timeout` 之一，Axcutor 统一采用 `start_to_close_timeout` 以允许任务在队列中持久等待离线 Worker 恢复）。 |
| **`max_call_depth`** | **`64`** | 正整数 $\ge 1$。调用树深度硬护栏（根图 `depth = 1`，每进入一层内联 `graph` 或 `use: workflow:*` 递增 `1`，`for_each` 迭代帧保持父帧深度）。超出时抛不可恢复的 `StackOverflowError`。 |
| **`child_workflow_history_events`** | **`None`** | 可选正整数 $\ge 1$。生产环境保持 `None`，由 Temporal 原生 `workflow.info().is_continue_as_new_suggested()` 在 `use: workflow:*` 调用边界自动切换为 Child Workflow；测试中可设为小整数（如 `1`）以确定性地验证跨 Child Workflow 边界行为。 |
| **`task_queues`** | **`{}`** | `dict[str, str]`（字符串键、非空字符串值）。按 Activity 型 `run_spec["executor"]` 名称将特定执行器路由至独立 Temporal Task Queue；缺省时沿用当前 Workflow 所在的默认队列。 |

2. **宿主与 Worker 进程级可覆盖默认参数（进程本地配置，非重放状态）**：
   * **`ExecutionService` 的 `rpc_timeout` 参数（内部保存为 `self._timeout`）**：构造默认值为 `timedelta(seconds=5)`（允许宿主实例化 `ExecutionService(..., rpc_timeout=...)` 时覆盖）。通过 `_call()` 中的 `asyncio.timeout(self._timeout.total_seconds())` 与 Temporal `rpc_timeout=self._timeout` 双重包裹单次后端 RPC 操作（涵盖 `start_workflow`、`describe`、`cancel`、`query` 及已关闭执行的 `result()`；注意 `_get()` 递归拉取多个 Child Workflow 快照时按每次 RPC 独立计算预算，而 `ExecutionService.signal()` 则在根 `describe` 之后用单个顶层 `asyncio.timeout(self._timeout.total_seconds())` 统一约束跨 Child Workflow 寻址循环与 `execute_update("deliver_signal")` 总耗时；`ExecutionService.wait()` 中间的 `handle.result()` 故意不设该预算以支持长轮询等待），超时统一映射为 HTTP `503 runtime_unavailable`。
   * **`create_worker(..., **kwargs)` 透传的 Temporal SDK Worker 资源参数**：生产默认不硬编码覆盖，直接沿用 Temporal Python SDK（`1.35.0`）原生默认值——包括 `max_heartbeat_throttle_interval=timedelta(seconds=60)`、`default_heartbeat_throttle_interval=timedelta(seconds=30)`、`max_concurrent_activities=None` 与 `max_concurrent_workflow_tasks=None`（在未传 `tuner` 时由底层 `sdk-core` `FixedSizeSlotSupplier` 默认各分配 `100` 个槽位）、`max_cached_workflows=1000`、`workflow_task_poller_behavior` 与 `activity_task_poller_behavior`（默认均为 `PollerBehaviorSimpleMaximum(maximum=5)`）、`graceful_shutdown_timeout=timedelta(0)`（CLI Worker 显式传入 `20s`，Playground Worker 显式传入 `5s` 及 `max_heartbeat_throttle_interval=timedelta(milliseconds=200)`）。调用方与测试夹具可通过 `**kwargs` 按需覆盖。

#### 第三层：编译器、编解码与容器/前端内部固定常量（纯内部护栏，不暴露配置）

以下机制属于防止栈溢出、精度丢失或进程僵死的固定实现常量，不提供运行期配置项以避免过度工程化：
* **源码与 JSON 解析深度上限**：Rust 编译器源码解析深度 `MAX_SOURCE_DEPTH = 128`、内核模型 JSON 解析深度 `MAX_MODEL_DEPTH = 132`（`crates/axcutor-core/src/source.rs`）；前端无损 JSON 解析器深度上限 `512`（`playground/src/utils/losslessJson.ts`）。
* **Python 无损大整数直通阈值**：
  * `codec.loads` 对整数数字 token 字符长度 `> 4000`（含负号）的值保留为 `simplejson.RawJSON`；
  * `codec.dumps`（内部 `_json_ready`）对 `value.bit_length() > 13000` 的 `int` 通过 `RawJSON(str(Decimal(value)))` 编码；
  * `builtin.py` 在输入校验（`_coerce_transport_integer`）中将 `RawJSON` 解码还原为 Python `int`，并在 `synthesize_contract_module` 的内部 `annotation` 函数中仅对 `value.bit_length() < 13000` 的整数枚举生成 `typing.Literal`（即 `bit_length() >= 13000` 时跳过 `Literal` 生成，仅通过 `Decimal` 字符串枚举校验器比对），从而在不修改 Python 全局 `sys.set_int_max_str_digits(4300)` 的前提下实现任意精度整数无损传输。
* **Workflow 内部冷启动 `signal` 初始化定时器**：`AxcutorInterpreterWorkflow._wait_signal` 在首次无外层截止时间作用域挂起（`not self._signal_initialized and not self._deadline_scope.get()`）时创建的共享 `workflow.sleep(timedelta(milliseconds=1))` 定时器（`self._signal_init_timer`），用于规避 Temporal SDK `#2080` 首任务无命令时的冷 Update 校验问题，不在投递路径上产生等待阻塞。
* **Playground 协调器与容器编排固定时序**：
  * `Playground`（`playground/backend/src/axcutor_playground/coordinator.py`）通过单实例 `execution_id` 与本地 `_inflight` 计数器实施单根执行准入控制；`_wait_until_done` 在遇到 `RuntimeUnavailable` / `ExecutionNotFound` 时固定 `await asyncio.sleep(1)` 重试，并在 `executions.wait()` 返回后等待本地 `self._drained` 排空完成才释放 `execution_id`；`Playground.close()` 中发起取消请求预算为 `5s`、等待 `_completion` 完成的预算为 `10s`。
  * 前端状态轮询：`playground/src/composables/useExecution.ts` 执行状态轮询间隔 `300ms`；`useWorkspace.ts` 工作区轮询间隔 `300ms`（busy）/ `1000ms`（idle）。
  * 容器编排（`compose.yaml`）：`api` 与 `worker` 服务 `stop_grace_period: 30s`、`playground` 服务 `stop_grace_period: 35s`；`temporal` 服务健康检查 `interval: 5s, timeout: 5s, retries: 12, start_period: 10s`。

---

### 决策 2：为什么 Activity 型 `run` 节点默认必须保留 `heartbeat_timeout` 且维持应用层截止时间看门狗

在收敛配置时，两个核心正确性问题是：
1. **能否默认关闭 Temporal 的 `heartbeat_timeout`（设为 `None`）？**
2. **既然 Temporal Server 与 SDK Core 已有 `start_to_close_timeout` 计时器，能否删掉 `activities.py` 内部的本地 `deadline` 协程与 `expires_at` 复查？**

答案均为 **不能**，二者各自承担不可替代的职责：

1. **为什么必须默认开启 `heartbeat_timeout`（不可为 `None`）并保持周期性心跳上报**：
   * **防止 Worker 物理崩溃导致已启动的 Activity 悬挂 7 天**：未显式声明 `timeout_ms` 的 Activity 型 `run` 节点默认使用 `activity_timeout_ms = 7 天` 作为 `start_to_close_timeout`。若未设置 `heartbeat_timeout`，一旦承载正在运行 Activity 的 Worker 进程发生物理崩溃（OOM、`SIGKILL`、宿主机掉电），Temporal Server 没有任何心跳超时依据判定该 Activity 已死亡，只能等待 7 天的 `start_to_close_timeout` 到期后才能触发重试或失败。
   * **驱动按超时比例自适应的 SDK 节流窗口并搭载 `WAIT_CANCELLATION_COMPLETED` 取消信号**：Axcutor 所有 Activity 均采用 `ActivityCancellationType.WAIT_CANCELLATION_COMPLETED`。在 Temporal 协议中，当父工作流或并发分支发起取消（且 Activity 尚未到达本地超时）时，Worker 唯有通过周期性调用 `activity.heartbeat()` 并实际发出 `RecordActivityTaskHeartbeat` RPC，才能从 Server 响应中读取 `cancel_requested` 标志并唤醒 `activity.wait_for_cancelled()`（注：即便未设置 `heartbeat_timeout`，SDK 仍可按 `default_heartbeat_throttle_interval = 30s` 发送心跳并搭载取消信号，但开启 `heartbeat_timeout` 可使 `sdk-core` 走 $\min(\text{effective\_heartbeat\_timeout} \times 0.8,\ \text{max\_heartbeat\_throttle\_interval})$ 分支，使节流窗口随心跳超时和节点超时自动收紧）。
2. **为什么 `RunActivities.execute_run` 必须同时保留本地 `deadline` 协程与 `expires_at` 单调时钟校验**：
   * 尽管 Temporal SDK Rust Core（`sdk-core`）内部也为 `start_to_close_timeout` 和 `heartbeat_timeout` 维护了本地超时计时器，但 SDK Core 的 `start_to_close` 本地计时器默认带有额外的缓冲延迟（`local_timeout_buffer_for_activities = 5s`），且仅触发通用 Activity 取消，不会主动区分业务 `AbortSignal.reason` 是 `"timeout"` 还是 `"cancelled"`，也无法阻止业务协程在未让出事件循环的同步阶段返回结果；
   * `RunActivities.execute_run`（`python/axcutor_runtime/src/axcutor_runtime/activities.py`）在本地同时启动 `business`、`deadline = asyncio.create_task(asyncio.sleep(timeout))`、`cancelled = asyncio.create_task(activity.wait_for_cancelled())` 与 `heartbeat = asyncio.create_task(heartbeats())` 四个协程：
     * 一旦 `deadline` 到期且事件循环获得调度，无需等待 SDK 缓冲延迟或服务端往返，立即在 Worker 进程内置位 `signal._abort("timeout")`，并在排空后抛出 `ApplicationError("attempt deadline exceeded", type="timeout")`；
     * 若 `business` 在截止时间前返回，随后经过了同步的 JSON 序列化与 Rust `validate_run_contract` 契约校验（若序列化或契约校验本身失败则立即按确定性的 `contract` / `EngineError` 报错，避免将契约错误误导性地包装为可重试的 `timeout`），在契约校验通过准备提交结果前，第 147 行再次执行 `if loop.time() >= expires_at:` 单调时钟复查——若同步校验耗时跨过了截止时间，仍会置位 `signal._abort("timeout")` 并拒绝提交当前尝试迟到的成功结果；
     * 无论通过何种路径退出，`finally` 块均执行 `await cancel_and_drain(tasks)` 等待**当前尝试在本地 Worker 进程中拥有的协程任务**完成合作式清理（注：本地 `cancel_and_drain` 仅约束当前尝试自身的协程树与结果提交，不提供跨尝试或跨进程的分布式互斥锁；若旧尝试在合作式清理阶段发生不可取消的长阻塞而 Server 已因超时启动下一次重试尝试，外部副作用的幂等与去重仍由具体执行器及外部系统按 Temporal 至少一次执行语义负责）。

---

### 决策 3：恢复 Temporal SDK 原生客户端节流机制（`0.8 * effective_heartbeat_timeout`）并隔离交互/测试环境

Temporal 官方 SDK（`temporalio.worker.Worker` 底层的 Rust Core `sdk-core`）内置了客户端心跳节流器（Client-Side Heartbeat Throttler）：
* 根据 `temporalio.worker.Worker` 与 `sdk-core` 的实现，客户端心跳节流周期 $C$ 的完整分段计算公式为：

  $$
  C = \begin{cases}
  \min(\text{effective\_heartbeat\_timeout} \times 0.8,\ \text{max\_heartbeat\_throttle\_interval}) & \text{if } \text{effective\_heartbeat\_timeout} > 0 \\
  \min(\text{default\_heartbeat\_throttle\_interval},\ \text{max\_heartbeat\_throttle\_interval}) & \text{otherwise}
  \end{cases}
  $$

  其中：
  1. `temporalio.worker.Worker` 的原生默认值为 `default_heartbeat_throttle_interval = 30s`、`max_heartbeat_throttle_interval = 60s`；
  2. $\text{effective\_heartbeat\_timeout}$ 是 Temporal Server 归一化后下发给 Worker 的有效心跳超时（Server 会自动将 `heartbeat_timeout` 截断在有效 `start_to_close_timeout` 以内，即 $\text{effective\_heartbeat\_timeout} = \min(\text{heartbeat\_timeout},\ \text{start\_to\_close\_timeout})$）；
  3. 由于 `AxcutorInterpreterWorkflow._run_activity` 始终传入非空的 `heartbeat_timeout`，在 Axcutor 中始终命中第一条分支（`default_heartbeat_throttle_interval` 实际不会参与计算）。
* **节流器与应用层唤醒协程的协同机制**：
  当 `RunActivities.execute_run` 启动时，$t=0$ 的首次 `activity.heartbeat(...)` 会立即发出并开启时长为 $C$ 的节流窗口；在窗口期内，应用层 `heartbeats()` 协程（每 `heartbeat_interval_ms` 唤醒一次）的后续调用会在 `sdk-core` 内存中记录/覆盖待发送的挂起心跳（pending heartbeat details）而不会立即触发网络 RPC；待节流定时器在 $t=C$ 到期时，**若窗口期内已有挂起的待发送心跳**，`sdk-core` 才会自动将其发送至 Temporal Server。因此，**实际心跳上报与取消信号感知延迟同时受限于应用层唤醒周期 `heartbeat_interval_ms` 与 SDK 节流周期 $C$**（单独缩小其中一个而保持另一个为 `10s` 或 `24s` 均无法实现快速取消送达）。
* **生产环境、交互式 `Playground` 与测试环境的分层节流策略**：
  1. **生产环境（`create_worker` 默认行为）**：`worker.py` 中的 `create_worker` 移除硬编码的 `100ms` 节流参数，默认直接沿用 Temporal SDK 原生配置（配合 `heartbeat_timeout_ms = 30000` 与 `heartbeat_interval_ms = 10000`，对于超时预算 $\ge 30\text{s}$ 的长耗时 Activity，稳态节流窗口为 $\min(30\text{s} \times 0.8,\ 60\text{s}) = 24\text{s}$，相比原先应用层每秒触发 1 次未节流 RPC 的生产默认行为，稳态心跳 RPC 频率降至原来的 $\frac{1}{24}$，减少约 $95.8\%$ 的心跳写负载）；
  2. **交互式 `Playground`（`coordinator.py`）**：由于 `Playground` 是面向单用户的 Web 交互式工作台，其 `Playground.close()` 的 `_completion` 等待阶段设有 `10s` 预算，且 Web UI 点击 Cancel 按钮（及 `browser_smoke.mjs` 的 `15s` 断言、`test_playground.py` 的 `5s` 清理断言）要求快速取消送达，因此在 `Playground.__init__` 构造 `ExecutionService` 时显式传入 `options={"heartbeat_interval_ms": 200}`（保持默认 `30000ms` 的 `heartbeat_timeout_ms` 不变，避免用户交互脚本短暂同步计算误触发心跳超时），**同时**在 `Playground.start()` 调用 `create_worker` 时与已有的 `graceful_shutdown_timeout=timedelta(seconds=5)` 并列传入 `max_heartbeat_throttle_interval=timedelta(milliseconds=200)`（注：在事件循环与网络正常时这保证 `~200ms` 级的取消信号送达，而执行终态、工作区 `self._drained` 解锁及停机完成仍取决于业务协程合作式清理的耗时）；
  3. **自动化测试套件（`tests/`）**：保持测试夹具 `options` 中的 `heartbeat_interval_ms = 20, heartbeat_timeout_ms = 1000`（既保留 1 秒心跳超时防抖余量以防止 CI 机器抖动误触发 `HeartbeatTimeout`，又以 `20ms` 唤醒周期持续填充挂起心跳），并在需要百毫秒级取消送达的测试 `create_worker` 调用处通过 `**kwargs` 显式传入 `max_heartbeat_throttle_interval=timedelta(milliseconds=100)`。

---

### 决策 4：调整生产默认心跳参数并实现 `heartbeat_interval_ms` 无截断线性自适应缩放

1. **生产环境默认值调整（直接切换，不引入 `workflow.patched` 版本分支）**：
   * `heartbeat_timeout_ms`：由 `10000`（10 秒）调整为 **`30000`（30 秒）**。当 Worker 发生硬崩溃时，Temporal Server 常规在 30 秒超时窗口内即可判定超时并触发重试或回退；配合 SDK 原生 `0.8` 节流系数，当节点超时预算 $\ge 30\text{s}$ 时，单个长耗时 Activity 在稳态下的心跳 RPC 节流窗口为 $24\text{s}$，同时放宽了业务协程短暂同步阻塞误触发 `HeartbeatTimeout` 的安全窗口；
   * `heartbeat_interval_ms`：由 `1000`（1 秒）调整为 **`10000`（10 秒，即 `heartbeat_timeout_ms // 3`）**。作为应用层协程唤醒周期，保持在 `heartbeat_timeout_ms` 的 $\frac{1}{3}$ 水平，既确保在 `0.8 * heartbeat_timeout` 的 SDK 节流窗口到期前始终有待发送的心跳详情供 `sdk-core` 刷新，又避免了过密的 Python 协程空转唤醒。
2. **仅配置 `heartbeat_timeout_ms` 时的无截断线性自适应缩放**：
   在 `AxcutorInterpreterWorkflow._parse_options(options)` 中，若调用方在 `options` 中仅显式传入了 `heartbeat_timeout_ms`（例如设为 `1000`、`60` 或 `120000`），而未显式传入 `heartbeat_interval_ms`，当 `"heartbeat_timeout_ms" in options and "heartbeat_interval_ms" not in options` 时，在执行正整数校验后、执行 `<` 比较前，自动按 $\frac{1}{3}$ 线性比例推导 `heartbeat_interval_ms`（无硬编码上限截断）：

   $$
   \text{heartbeat\_interval\_ms} = \max\left(1,\ \left\lfloor\frac{\text{heartbeat\_timeout\_ms}}{3}\right\rfloor\right)
   $$

   * **数学不变量与边界行为**：
     * 对于任意整数 $\text{heartbeat\_timeout\_ms} \ge 2$，均有 $1 \le \text{heartbeat\_interval\_ms} < \text{heartbeat\_timeout\_ms}$ 严格成立（例如 `T=2 -> I=1`，`T=60 -> I=20`，`T=1000 -> I=333`，`T=30000 -> I=10000`，`T=120000 -> I=40000`）；
     * 当 `heartbeat_timeout_ms == 1` 时，由于正整数毫秒域内不存在满足 $1 \le I < 1$ 的整数间隔，推导出的 `I=1` 会在随后的 `result["heartbeat_interval_ms"] >= result["heartbeat_timeout_ms"]` 校验中明确抛出 `ValueError("heartbeat interval must be less than heartbeat timeout")`。

---

## 三、 具体实现变更规范

### 3.1 `worker.py`：移除硬编码的 `100ms` 节流参数

在 `python/axcutor_runtime/src/axcutor_runtime/worker.py` 中，移除 `create_worker` 内硬编码的 `max_heartbeat_throttle_interval` 与 `default_heartbeat_throttle_interval`（以及不再需要的 `timedelta` 导入）：

```python
def create_worker(
    client: Client,
    task_queue: str,
    executors: Mapping[str, Executor] | None = None,
    *,
    module_mounts: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> Worker:
    """Register both Python executors; copy mount configuration outside replay."""
    activities = RunActivities(executors, module_mounts=module_mounts)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[AxcutorInterpreterWorkflow],
        activities=[activities.execute_run],
        workflow_runner=workflow_runner(),
        **kwargs,
    )
```

### 3.2 `workflow.py`：更新默认心跳参数与无截断单参数推导逻辑

在 `python/axcutor_runtime/src/axcutor_runtime/workflow.py` 中，更新 `AxcutorInterpreterWorkflow._parse_options`：

```python
    @staticmethod
    def _parse_options(options: JsonObject) -> JsonObject:
        result: JsonObject = {
            "activity_timeout_ms": 7 * 24 * 60 * 60 * 1000,
            "heartbeat_interval_ms": 10000,
            "heartbeat_timeout_ms": 30000,
            "max_call_depth": 64,
            "task_queues": {},
            "child_workflow_history_events": None,
        }
        if not isinstance(options, dict) or options.keys() - result.keys():
            raise ValueError("unknown runtime options")
        result.update(deepcopy(options))
        for name in (
            "activity_timeout_ms",
            "heartbeat_interval_ms",
            "heartbeat_timeout_ms",
            "max_call_depth",
        ):
            if type(result[name]) is not int or result[name] <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if "heartbeat_timeout_ms" in options and "heartbeat_interval_ms" not in options:
            result["heartbeat_interval_ms"] = max(1, result["heartbeat_timeout_ms"] // 3)
        if result["heartbeat_interval_ms"] >= result["heartbeat_timeout_ms"]:
            raise ValueError("heartbeat interval must be less than heartbeat timeout")
        queues = result["task_queues"]
        if not isinstance(queues, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not v for k, v in queues.items()
        ):
            raise ValueError("task_queues must map executor names to nonempty task queues")
        threshold = result["child_workflow_history_events"]
        if threshold is not None and (type(threshold) is not int or threshold <= 0):
            raise ValueError("child_workflow_history_events must be a positive integer")
        return result
```

### 3.3 `Playground` 与测试夹具隔离规范

1. **交互式 `Playground`（`playground/backend/src/axcutor_playground/coordinator.py`）**：
   必须同时配置应用层心跳唤醒间隔与 Worker 客户端节流上限（二者为“且”关系）：在 `Playground.__init__` 中构造 `ExecutionService` 时传入 `options={"heartbeat_interval_ms": 200}`（保留默认 `30000ms` 的 `heartbeat_timeout_ms` 防抖窗口），**同时**在 `Playground.start()` 的 `create_worker` 中传入 `max_heartbeat_throttle_interval=timedelta(milliseconds=200)`，使 Web UI 取消请求、`Playground.close()` 的 `10s` 完成等待阶段、`test_playground.py` 的 `5s` 清理断言以及 `browser_smoke.mjs` 的 `15s` 取消断言在事件循环与网络正常时实现 `~200ms` 级的亚秒级取消信号送达。
2. **自动化测试套件（`tests/conftest.py`、`tests/host/test_service.py`、`tests/host/test_http.py`、`playground/backend/tests/test_playground.py`）**：
   保持测试 `options` 中的 `heartbeat_interval_ms = 20, heartbeat_timeout_ms = 1000`（使应用层协程每 `20ms` 刷新一次待发送心跳以跑满 `100ms` 节流窗口，同时维持 `1000ms` 心跳超时防抖余量），并在需要快速取消的 `create_worker` 调用点显式传入 `max_heartbeat_throttle_interval=timedelta(milliseconds=100)`（无需传入不参与计算的 `default_heartbeat_throttle_interval`）：
   ```python
   async with create_worker(
       server.client,
       queue,
       registry,
       max_heartbeat_throttle_interval=timedelta(milliseconds=100),
   ):
       ...
   ```
   全仓需注入或回归验证的具体位置清单：
   * `tests/conftest.py`：第 79 行公共 `execution` 夹具中的 `create_worker`；
   * `tests/runtime/test_executor_dispatch.py`：补充 `_parse_options` 生产默认值（`10000 / 30000`）、单参数无截断缩放（如 `T=60 -> I=20`、`T=1000 -> I=333`、`T=120000 -> I=40000`）、边界值（`T=1` 拒绝 vs `T=2` 接受）以及 `create_worker` `**kwargs` 透传测试；
   * `tests/host/test_service.py`：第 214 行（定义于第 171 行 `test_live_frame_tree_and_cooperative_cancel_include_all_descendants`）及其余涉及运行中 Activity 取消的 `create_worker` 调用点；
   * `tests/host/test_http.py`：第 230 行（定义于第 208 行 `test_live_routes_conflicts_cancellation_and_terminal_idempotency`）及其余涉及运行中 Activity 取消的 `create_worker` 调用点；
   * `playground/backend/tests/test_playground.py`：第 390 行（定义于第 372 行 `test_execution_routes_preserve_ids_lossless_frames_and_cancellation`）中的独立 `create_worker` 调用点，**以及第 377 行该测试独立构造的 `ExecutionService(server.client, queue, options={"heartbeat_interval_ms": 20, "heartbeat_timeout_ms": 1000})`**（因该测试绕过了 `Playground` 实例直接构造 `ExecutionService`，必须同步注入快速心跳 `options`，否则仍会按 `10s` 默认间隔唤醒；注：第 414 行新增的 `test_playground_signal_route_uses_execution_contract` 仅执行 `signal` 节点而不涉及 Activity 心跳取消，无需注入）。

---

## 四、 变更影响模块清单

| 模块路径 | 变更内容摘要 |
| :--- | :--- |
| `python/axcutor_runtime/src/axcutor_runtime/worker.py` | 从 `create_worker` 中移除硬编码的 `max_heartbeat_throttle_interval=timedelta(milliseconds=100)` 与 `default_heartbeat_throttle_interval=timedelta(milliseconds=100)`，恢复 Temporal SDK 原生客户端节流。 |
| `python/axcutor_runtime/src/axcutor_runtime/workflow.py` | 将 `_parse_options` 中的默认 `heartbeat_interval_ms` 调整为 `10000`、`heartbeat_timeout_ms` 调整为 `30000`；当 `options` 仅指定 `heartbeat_timeout_ms` 时自动按 `max(1, heartbeat_timeout_ms // 3)` 无截断推导 `heartbeat_interval_ms`。 |
| `playground/backend/src/axcutor_playground/coordinator.py` | 在 `Playground.__init__` 的 `ExecutionService` 中传入 `options={"heartbeat_interval_ms": 200}`，并在 `Playground.start()` 的 `create_worker` 中传入 `max_heartbeat_throttle_interval=timedelta(milliseconds=200)`，确保 Web UI 取消操作与 `Playground.close()` 的 `10s` 完成等待阶段快速送达取消信号。 |
| `tests/conftest.py`、`tests/host/test_service.py`、`tests/host/test_http.py`、`tests/runtime/test_executor_dispatch.py`、`playground/backend/tests/test_playground.py` | 在测试创建 `Worker`（及 `test_playground.py` 独立 `ExecutionService`）时显式隔离测试环境的快速取消配置，并补充 `_parse_options` 单参数推导与边界单元测试。 |
