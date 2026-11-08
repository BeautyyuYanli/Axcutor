# RFC-0016: Temporal Activity 心跳配置修正与客户端节流机制规范

* **状态**：Proposed
* **所属分类**：运行时配置修正、Temporal Activity 生命周期管理与可观测性（Runtime Configuration, Activity Heartbeat & Liveness Management）
* **前置关联提案**：
  * [`RFC-0011: Axcutor 编译器前后端与 Temporal 运行时参考实现架构`](./0011-compiler-and-temporal-runtime-architecture.md)
  * [`RFC-0012: 结构化 run 执行器分派机制、移除自然语言变量正则提取与 builtin 可信 Python 执行器规范`](./0012-run-executor-and-builtin-python.md)
* **核心解决的问题**：
  1. **消除生产环境 Worker 硬编码 `100ms` 心跳节流上限导致的高频 RPC 放大问题**：移除 `worker.py` 中 `create_worker` 硬编码的 `max_heartbeat_throttle_interval=timedelta(milliseconds=100)` 与 `default_heartbeat_throttle_interval=timedelta(milliseconds=100)`，恢复 Temporal SDK 原生的客户端节流机制（$\min(\text{heartbeat\_timeout} \times 0.8,\ \text{max\_heartbeat\_throttle\_interval})$），将测试专用的高频节流参数隔离至测试夹具（`tests/conftest.py`）；
  2. **确立符合 Temporal 最佳实践的默认心跳参数与自适应缩放规则**：将 `workflow.py` 中 `default_options` 的生产默认值由 `heartbeat_interval_ms: 1000, heartbeat_timeout_ms: 10000` 调整为 `heartbeat_interval_ms: 5000, heartbeat_timeout_ms: 30000`，并在用户仅自定义 `heartbeat_timeout_ms` 时自动按比例缩放 `heartbeat_interval_ms`；
  3. **明确 Temporal Activity 心跳在 Axcutor 运行时中的职责边界**：阐明为什么默认必须开启 `heartbeat_timeout`（防止 Worker 崩溃时卡死在长达 7 天的 `activity_timeout_ms`，并保障 `WAIT_CANCELLATION_COMPLETED` 取消信号及时送达），同时明确禁止将 `activity.heartbeat()` 滥用为高频状态同步通道或预写日志（WAL）。

---

## 一、 问题背景与根因溯源

### 问题 16.1：测试环境的毫秒级取消响应需求污染了生产 Worker 与默认工作流配置

在 [`RFC-0011`](./0011-compiler-and-temporal-runtime-architecture.md) 的初始实现（提交 `759936c7`）中，`axcutor_runtime` 在两处核心路径引入了极高频的心跳参数：

1. **`worker.py` 硬编码 `100ms` 节流上限**：
   在 `python/axcutor_runtime/src/axcutor_runtime/worker.py` 的 `create_worker` 中，无条件向 `temporalio.worker.Worker(...)` 传入了：
   ```python
   max_heartbeat_throttle_interval=timedelta(milliseconds=100),
   default_heartbeat_throttle_interval=timedelta(milliseconds=100),
   ```
2. **`workflow.py` 默认每秒发送一次心跳**：
   在 `python/axcutor_runtime/src/axcutor_runtime/workflow.py` 的 `_parse_options` 中，将全局默认参数设置为：
   ```python
   "heartbeat_interval_ms": 1000,
   "heartbeat_timeout_ms": 10000,
   ```

**历史引入原因**：
在 Temporal 的架构中，正在执行的 Activity 唯有通过向 Temporal Server 发送 `RecordActivityTaskHeartbeat` RPC 并读取响应中的 `cancel_requested` 标志，才能感知到父工作流或并发分支的取消请求（`ActivityCancellationType.WAIT_CANCELLATION_COMPLETED`）。
为了使集成测试套件（`tests/conftest.py` 中的 `fast_options: heartbeat_interval_ms=20, heartbeat_timeout_ms=1000` 以及 `tests/test_cancellation.py` 中的取消传播测试）能够在百毫秒内完成取消断言，实现者当时直接在生产函数 `create_worker` 中将 Temporal SDK 的客户端节流器硬编码压低到了 `100ms`（绕过了 Temporal SDK 默认 `default=30s, max=60s` 的客户端合并节流机制）。

### 问题 16.2：绕过客户端节流的高频心跳违背 Temporal 设计规范并引发写放大

在生产环境的 Temporal 集群中，将 Worker 节流间隔硬编码为 `100ms` 并配合 `1000ms` 的循环心跳会产生严重性能隐患：
* **Mutable State 写放大与锁争用**：每一次实际发往 Temporal Server 的 `RecordActivityTaskHeartbeat` RPC 都要求 History Service 获取对应 Workflow Execution 的内存锁，并更新持久化存储中的 Mutable State 记录；
* **并发节点下的雪崩效应**：当工作流通过 `for_each` 或 `fork` 并发调度数十至数百个长耗时节点（如批处理、LLM 调用或外部轮询）时，每个活跃 Activity 每秒触发一次未节流的 Server RPC，会导致 Temporal History 分片承受大量无效写负载；
* **职责边界混淆**：Temporal 官方设计中，`activity.heartbeat()` 的职责仅限于 **长耗时任务的存活探测（Liveness Detection）**、**取消信号搭载（Cancellation Delivery）** 与 **粗粒度断点续传游标（Checkpoint Details）**，绝不能被当作高频状态同步通道或毫秒级预写日志（WAL）使用。

---

## 二、 核心设计决策

### 决策 1：为什么默认必须保留 `heartbeat_timeout`（不可缺省为 `None`）

在修正高频心跳时，一个关键问题是：**能否默认关闭 `heartbeat_timeout`（即不填或设为 `0`）？**
答案是 **不能**。原因在于 Axcutor 的超时模型与取消模型：

1. **防止 Worker 物理崩溃导致工作流悬挂 7 天**：
   在 Axcutor 中，`default_options.activity_timeout_ms`（映射为 Temporal 的 `start_to_close_timeout`）默认值为 **`604,800,000 ms`（7 天）**，以容纳长耗时 AI Agent 推理、外部异步任务等待或大批量处理。
   如果未设置 `heartbeat_timeout`（即 `heartbeat_timeout=None`），一旦承载正在运行 Activity 的 Worker 进程发生物理崩溃（OOM、`SIGKILL`、宿主机宕机），Temporal Server 将 **没有任何心跳超时依据** 来判定该 Activity 已死亡，只能等待整整 7 天的 `start_to_close_timeout` 届满后才能触发重试或超时失败，导致工作流在生产环境中实质性卡死。
2. **保障 `WAIT_CANCELLATION_COMPLETED` 取消信号在合理窗口内送达**：
   Axcutor 在调度所有 Activity 时均采用 `cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED`，以便在 `fork` 分支快败（Fail-Fast）或工作流取消时等待受影响节点完成资源清理。若未配置 `heartbeat_timeout` 且不发送心跳，正在运行的 Activity 将永远无法收到 Temporal Server 下发的 `asyncio.CancelledError`。

因此，Axcutor 必须 **默认开启 `heartbeat_timeout` 并维持后台 `_heartbeat_loop` 协程**，使业务节点代码（`builtin` 或 `module`）无需手动编写 `activity.heartbeat()` 即可天然获得存活探测与取消感知能力。

### 决策 2：恢复 Temporal SDK 原生客户端节流机制（`0.8 * heartbeat_timeout`）

Temporal 官方 SDK（`temporalio.worker.Worker` 底层的 Rust Core `sdk-core`）内置了成熟的客户端心跳节流器（Client-Side Heartbeat Throttler）：
* 当配置了 `heartbeat_timeout` 时，SDK 客户端实际允许向 Server 发送心跳 RPC 的最小节流周期计算公式为：

  $$
  \text{throttle\_interval} = \min(\text{heartbeat\_timeout} \times 0.8,\ \text{max\_heartbeat\_throttle\_interval})
  $$

  其中 `temporalio.worker.Worker` 的原生默认值为 `default_heartbeat_throttle_interval = 30s`、`max_heartbeat_throttle_interval = 60s`。
* 这意味着，即便 Axcutor 的 `_heartbeat_loop` 在应用层每隔数秒调用一次 `activity.heartbeat()`，SDK 的 Rust Core 也会在本地静默吸收多余调用，仅在首个心跳时刻及之后每隔 $\min(0.8 \times \text{heartbeat\_timeout}, 60\text{s})$ 才真正向 Temporal Server 发起一次网络 RPC，既保证了超时前必定送达心跳，又彻底消除了对 Server 的高频 RPC 冲击。

因此，`worker.py` 中的 `create_worker` **严禁硬编码覆盖** `max_heartbeat_throttle_interval` 与 `default_heartbeat_throttle_interval`，必须直接沿用 Temporal SDK 的原生默认值；仅在集成测试夹具（`tests/conftest.py`）中通过 `**kwargs` 显式注入 `100ms` 节流上限以加速测试运行。

### 决策 3：调整生产默认心跳参数并支持 `heartbeat_interval_ms` 自适应缩放

1. **生产环境默认值调整**：
   * `heartbeat_timeout_ms`：由 `10000`（10 秒）调整为 **`30000`（30 秒）**。当 Worker 发生硬崩溃时，Temporal Server 最多在 30 秒内即可感知并触发重试或回退；配合 SDK 原生 `0.8` 节流系数，单个长耗时 Activity 对 Server 的实际心跳 RPC 频率不超过每 24 秒 1 次；
   * `heartbeat_interval_ms`：由 `1000`（1 秒）调整为 **`5000`（5 秒）**。作为应用层协程唤醒周期，5 秒开销极低，且始终保持在 `heartbeat_timeout_ms` 的 $\frac{1}{6}$ 水平，足以避免因 Python 事件循环短暂阻塞而误触发超时。
2. **仅配置 `heartbeat_timeout_ms` 时的自适应缩放**：
   若用户在 YAML 的 `default_options` 或节点 `options` 中仅显式调小了 `heartbeat_timeout_ms`（例如设为 `3000`），而未显式指定 `heartbeat_interval_ms`，若仍沿用默认的 `5000ms` 间隔会导致必然超时。因此，当 `heartbeat_timeout_ms` 被显式指定且 `heartbeat_interval_ms` 未被显式指定时，运行时自动按比例推导：

   $$
   \text{heartbeat\_interval\_ms} = \max\left(100,\ \min\left(5000,\ \left\lfloor\frac{\text{heartbeat\_timeout\_ms}}{3}\right\rfloor\right)\right)
   $$

---

## 三、 具体实现变更规范

### 3.1 `worker.py`：移除硬编码的 `100ms` 节流参数

在 `python/axcutor_runtime/src/axcutor_runtime/worker.py` 中，移除 `create_worker` 内硬编码的 `max_heartbeat_throttle_interval` 与 `default_heartbeat_throttle_interval`：

```python
def create_worker(
    client: Client,
    task_queue: str,
    worker_activities: ActivityExecutor | None = None,
    **kwargs: Any,
) -> Worker:
    if worker_activities is None:
        worker_activities = ActivityExecutor()
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[AxcutorWorkflow],
        activities=[
            worker_activities.execute_node,
            worker_activities.dispose_handles,
        ],
        # 使用 Temporal SDK 原生客户端心跳节流默认值（default=30s, max=60s，
        # 实际节流窗口为 min(heartbeat_timeout * 0.8, 60s)）。
        # 测试环境如需毫秒级取消响应，应通过 **kwargs 显式传入节流参数。
        **kwargs,
    )
```

### 3.2 `workflow.py`：更新默认心跳参数与推导逻辑

在 `python/axcutor_runtime/src/axcutor_runtime/workflow.py` 中，更新 `_parse_options`（以及节点级 `options` 合并逻辑）：

```python
def _parse_options(ir: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "workflow_timeout_ms": 0,
        "activity_timeout_ms": 604800000,
        "schedule_to_start_timeout_ms": 0,
        "heartbeat_interval_ms": 5000,
        "heartbeat_timeout_ms": 30000,
        "retry_policy": {
            "max_attempts": 3,
            "initial_interval_ms": 1000,
            "backoff_coefficient": 2.0,
            "max_interval_ms": 60000,
            "non_retryable_error_types": ["FatalError", "ValidationError"],
        },
    }
    raw = ir.get("default_options") or {}
    merged = {**defaults, **raw}
    if "retry_policy" in raw and isinstance(raw["retry_policy"], dict):
        merged["retry_policy"] = {**defaults["retry_policy"], **raw["retry_policy"]}

    if (
        "heartbeat_timeout_ms" in raw
        and "heartbeat_interval_ms" not in raw
        and merged["heartbeat_timeout_ms"] > 0
    ):
        merged["heartbeat_interval_ms"] = max(
            100, min(defaults["heartbeat_interval_ms"], merged["heartbeat_timeout_ms"] // 3)
        )
    return merged
```

同理，在 `_build_activity_config` 中，若单节点 `node_opts` 仅覆盖了 `heartbeat_timeout_ms` 而未覆盖 `heartbeat_interval_ms`，同样确保传入 Activity 的 `heartbeat_interval_ms` 不超过 `heartbeat_timeout_ms // 3`。

### 3.3 测试夹具隔离（`tests/conftest.py` 与 `tests/test_cancellation.py`）

在 `python/axcutor_runtime/tests/conftest.py`（及任何自行构造 `Worker` 的取消测试）中，显式向 `create_worker` 注入测试专用的 `100ms` 节流间隔，确保测试套件保持毫秒级执行速度且不影响生产默认值：

```python
worker = create_worker(
    client,
    task_queue,
    executor,
    max_heartbeat_throttle_interval=timedelta(milliseconds=100),
    default_heartbeat_throttle_interval=timedelta(milliseconds=100),
)
```

---

## 四、 变更影响模块清单

| 模块路径 | 变更内容摘要 |
| :--- | :--- |
| `python/axcutor_runtime/src/axcutor_runtime/worker.py` | 从 `create_worker` 中移除硬编码的 `max_heartbeat_throttle_interval=timedelta(milliseconds=100)` 与 `default_heartbeat_throttle_interval=timedelta(milliseconds=100)`，恢复 Temporal SDK 原生客户端节流。 |
| `python/axcutor_runtime/src/axcutor_runtime/workflow.py` | 将 `_parse_options` 中的默认 `heartbeat_interval_ms` 调整为 `5000`、`heartbeat_timeout_ms` 调整为 `30000`；当用户仅自定义 `heartbeat_timeout_ms` 时自动缩放 `heartbeat_interval_ms`。 |
| `python/axcutor_runtime/tests/conftest.py` | 在测试 Worker 夹具中通过 `**kwargs` 显式传入 `100ms` 节流上限，隔离测试加速配置。 |
| `manual/docs/reference/01-node-and-graph-fields.md` | 同步更新文档中 `heartbeat_interval_ms`（默认 `5000`）与 `heartbeat_timeout_ms`（默认 `30000`）的说明。 |
