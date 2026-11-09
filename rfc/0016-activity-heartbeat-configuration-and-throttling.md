# RFC-0016: Temporal Activity 心跳配置与节流

* **状态**：Proposed
* **关联提案**：[RFC-0011 运行时架构](./0011-compiler-and-temporal-runtime-architecture.md)、[RFC-0012 执行器 ABI](./0012-run-executor-and-builtin-python.md)

本提案收敛 Activity 心跳的默认值、参数推导与环境覆盖方式：生产 Worker 恢复 SDK 原生节流，Playground 与取消测试显式选择快速心跳。保持现有超时、重试和清理模型。

## 一、问题与范围

当前实现有两个相连的问题：

1. `create_worker` 将两个 SDK 心跳节流参数都硬编码为 `100ms`，使生产环境承担测试所需的高频 RPC；调用方通过 `**kwargs` 覆盖同名参数还会触发重复关键字错误。
2. `_parse_options` 默认使用 `heartbeat_interval_ms=1000`、`heartbeat_timeout_ms=10000`。仅将 timeout 调小到 `500ms` 时，未变的 interval 会使 `interval < timeout` 校验失败。

配置按所有权放置，不新增 DSL 字段、配置层或运行模式：

| 所有者 | 职责 |
| :--- | :--- |
| 工作流作者 | 通过既有 `timeout_ms`、`retry`、`graph.max_steps`、`for_each.concurrency` 表达业务控制流。 |
| 宿主提交的 `request["options"]` | 设置 Activity 心跳 timeout 与 interval；省略时使用生产默认值。 |
| Worker 创建方 | 通过既有 `create_worker(..., **kwargs)` 覆盖 SDK 节流参数，按部署环境选择 RPC 频率。 |

其他运行时选项、SDK 资源配额、编解码阈值、前端轮询和容器时序由各自模块维护，不在此另设规范。步数、调用深度和单次尝试超时见[运行护栏](../manual/spec/04-runtime-execution-model/03-guardrails-and-step-accounting.md)，重试见[异常处理规范](../manual/spec/04-runtime-execution-model/04-exception-handling-and-unwinding.md)。

## 二、心跳参数

`AxcutorInterpreterWorkflow._parse_options` 使用以下规则，参数只作用于 Activity 型 `run`，不作用于 [signal 等待](./0017-cooperative-signal-executor.md)：

| 参数 | 省略时的值 | 含义与约束 |
| :--- | :--- | :--- |
| `heartbeat_timeout_ms`（T） | `30000` | Server 心跳故障检测的请求值；正整数。 |
| `heartbeat_interval_ms`（I） | `max(1, T // 3)` | Activity 本地调用 `activity.heartbeat()` 的间隔；整数且 `1 <= I < T`。 |

先沿用运行时选项的统一正整数校验，再为省略的 I 推导默认值，最后校验 `I < T`。显式 I 原样保留；只设置 I 时，T 仍为 `30000`。不对推导结果另设上限或下限常量。

T 是请求配置值，Server 实际采用的心跳 timeout 不超过该 Activity 的单次尝试预算（`start_to_close_timeout`），见[心跳超时说明](https://docs.temporal.io/encyclopedia/detecting-activity-failures#heartbeat-timeout)。

```python
# 局部规则示意：options 为调用方传入的覆盖值，类型与范围已校验。
timeout = options.get("heartbeat_timeout_ms", 30000)
interval = options.get("heartbeat_interval_ms", max(1, timeout // 3))
if interval >= timeout:
    raise ValueError("heartbeat interval must be less than heartbeat timeout")
```

例如，`{}` 得到 `I=10000, T=30000`；只传 `T=1000` 得到 `I=333`；只传 `T=120000` 得到 `I=40000`。边界 `T=2` 得到 `I=1`，`T=1` 因最终不满足 `I < T` 而拒绝。

保留显式 I，是为了让交互环境在保留较宽故障检测窗口的同时加快取消响应。将 I 固定绑定为 T 的三分之一，会迫使快速取消场景同时缩短故障检测窗口，增加误判 Worker 失活的可能。

## 三、Worker 节流与环境选择

`create_worker` 删除硬编码的 `max_heartbeat_throttle_interval` 和 `default_heartbeat_throttle_interval`，让 `**kwargs` 直接透传。生产环境沿用 SDK 默认值；不增加 Axcutor 自己的节流器。

当前锁定的 Temporal Python SDK 默认最大节流间隔为 `60s`、无心跳 timeout 时的默认节流间隔为 `30s`，见 [SDK Worker 定义](https://github.com/temporalio/sdk-python/blob/1.35.0/temporalio/worker/_worker.py)。Axcutor 始终设置心跳 timeout，SDK 按有效 timeout 的 `0.8` 倍与最大节流间隔的较小值节流。应用层调用会被合并，不能将一次 `activity.heartbeat()` 等同于一次 RPC，见 [Temporal 心跳节流说明](https://docs.temporal.io/encyclopedia/detecting-activity-failures#throttling)。

| 环境 | I | T | Worker 最大节流间隔 |
| :--- | :--- | :--- | :--- |
| 生产默认 | `10000ms` | `30000ms` | SDK 默认 |
| Playground | `200ms` | `30000ms` | `200ms` |
| 需要快速取消的测试 | `20ms` | `1000ms` | `100ms` |

长耗时 Activity 在生产默认下的典型节流窗口约为 `24s`，降低 RPC 频率的代价是取消送达也可能等待数十秒。生产默认接受这一取舍；要求快速取消的宿主必须同时缩短 I 和 Worker 最大节流间隔。仅改其中一个不足以保证快速送达。

这一取舍也影响复合节点超时与并发分支失败。例如，内联 `graph` 节点的 `timeout_ms: 1000` 到期后，若后代 Activity 自身期限较长，该节点仍须等待其收到取消并完成清理，才进入后续 `catch` 或重试；祖先期限不会自动缩短 Activity 的心跳间隔或本地 deadline。

Playground 在构造 `ExecutionService` 时传入 `options={"heartbeat_interval_ms": 200}`，并在创建 Worker 时传入 `max_heartbeat_throttle_interval=timedelta(milliseconds=200)`。保留默认 T，避免为 UI 响应速度牺牲故障检测余量。

快速取消配置只约束心跳送达机会，不承诺执行终态或停机完成时间；事件循环、网络与业务清理耗时仍影响结果。短于 I 的尝试可能只发送首次心跳，仍由自身的本地 deadline 到期后中止，不额外按节点期限缩放心跳间隔。

## 四、超时与清理边界

保持 `RunActivities.execute_run` 的现有职责分工：

| 机制 | 必须保留的职责 |
| :--- | :--- |
| Server 心跳 timeout | Worker 崩溃后检测失活；不能只依赖缺省为 7 天的 `activity_timeout_ms`。超时后的重试仍由既有 `retry` 策略决定。 |
| 周期性心跳 | 让正在执行的 Activity 接收 Server 取消请求；继续使用 `WAIT_CANCELLATION_COMPLETED`。 |
| Activity 本地 deadline | 当前尝试预算到期且事件循环可调度时，置位 `AbortSignal`，原因为 `"timeout"`，并开始合作式清理。 |
| 提交前的单调时钟复查 | 同步序列化或契约校验跨过期限后，拒绝提交迟到的成功结果。 |
| `cancel_and_drain` | 退出时取消并等待当前尝试拥有的本地任务，丢弃已中止尝试的迟到结果。 |

心跳不取代业务期限或结果校验；本地清理也不提供跨尝试互斥。Server 超时后可能启动下一次尝试，外部副作用仍由执行器及外部系统负责幂等与去重。详细取消契约见[执行器 ABI](../manual/spec/05-executor-abi/01-run-executor-contract.md)。

## 五、默认值切换

`request["options"]` 持久化的是宿主实际提交的覆盖值。生产宿主通常提交 `{}`，缺省值由 Workflow 代码在执行或重放时补齐，并非提交时固化。

本次直接切换默认值，不引入 `workflow.patched` 分支。已有执行换用新代码重放后，后续新调度的 Activity 使用补齐后的新默认值；已调度 Activity 的请求与超时仍来自历史，包含其 Server 原生重试。已启动的 Child Workflow 因输入中携带完整选项，也保留原值。同一执行树可在过渡期间使用不同的心跳策略。

这一切换只改变 Activity 的参数与超时，不改变命令种类或控制流；Temporal 允许此类变更，见[重放兼容说明](https://docs.temporal.io/workflow-definition#deterministic-constraints)。回归验证应覆盖旧版本以缺省 options 产生的历史，而非只使用显式覆盖两个心跳参数的测试历史。

## 六、变更与验证范围

| 位置 | 变更或验证内容 |
| :--- | :--- |
| `python/axcutor_runtime/src/axcutor_runtime/worker.py` | 删除两个硬编码节流参数与无用导入；验证调用方可覆盖参数，未覆盖时沿用 SDK 默认。 |
| `python/axcutor_runtime/src/axcutor_runtime/workflow.py` | 修改心跳默认值与 I 的推导；覆盖缺省、仅传 T、仅传 I、显式双参数、整数类型及 `T=1/2` 边界。 |
| `playground/backend/src/axcutor_playground/coordinator.py` | 同时注入应用层 I 与 Worker 最大节流间隔；验证取消和关闭仍能完成合作式清理。 |
| `tests/conftest.py`、`tests/host/`、`playground/backend/tests/` | 公共 `execution` 夹具及需要快速取消的独立服务测试，显式成对设置请求与 Worker 参数，包含绕过 Playground 直接构造服务的测试。 |
| 运行时回归 | 保持超时通知、迟到成功丢弃、在途任务排空及历史重放验证；补充缺省配置跨版本历史重放和后续新调度采用新默认值的验证。 |

本提案仅修改上述配置路径；Activity 生命周期机制沿用既有实现，不复制完整函数作为第二份规范。
