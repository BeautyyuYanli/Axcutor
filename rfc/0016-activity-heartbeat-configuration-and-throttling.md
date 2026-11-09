# RFC-0016: 全栈执行预算、资源配额与心跳配置收敛

* **状态**：Proposed
* **关联提案**：[RFC-0011 运行时架构](./0011-compiler-and-temporal-runtime-architecture.md)、[RFC-0012 执行器 ABI](./0012-run-executor-and-builtin-python.md)、[RFC-0017 signal 等待](./0017-cooperative-signal-executor.md)

## 一、目标与收敛原则

本提案系统性梳理本仓 DSL、Workflow、Activity、宿主、Worker、编译器与编解码、Playground 前后端及容器编排中的 `max_*`、timeout、重试、并发、心跳和轮询机制。每项都明确目的、所有者、配置入口、默认值、生命周期与处置结论；完整盘点不等于新增配置项。

当前有两个需要直接修复的问题：`create_worker` 硬编码 `100ms` 心跳节流，既使生产承担测试所需的 RPC 频率，又阻止调用方通过 `**kwargs` 覆盖同名参数；心跳 interval 与 timeout 使用互不关联的默认值，仅调小 timeout 就可能违反 `interval < timeout`。

统一采用以下规则：

1. **按所有权放置**：业务预算写在 DSL；执行所需的运行选项进入持久化请求；进程资源与部署时序留给宿主；解析保护和表示策略保持内部常量。下面四类是现有入口的分类，不新增配置对象、配置中心或 DSL 字段。
2. **同一预算只声明一次**：Activity 的 Server 尝试期限与本地 deadline 从同一个有效预算派生；SDK 重试参数从 DSL `retry` 派生。不同职责的预算不机械合并。
3. **默认即可运行，覆盖必须有明确用途**：生产保留少量默认值；心跳 I 默认由 T 派生；Worker 配额沿用 SDK。需要快速取消的宿主显式配置对应入口。
4. **约束必须说明作用范围**：区分排队、单次尝试、整个节点激活、RPC、观察等待和停机；不把局部护栏解释为整个执行的时长或资源上限。

## 二、全栈机制矩阵与处置

以下为本提案采用的配置基线；未标明调整的默认值与预算语义保持现状。表中毫秒值为整数，正整数不包含布尔值；控制参数的后端消费边界见第四节。表内列出本仓实际使用的机制；SDK 其余能力继续归 SDK 管理，不复制依赖的全部内部参数。

### 2.1 DSL：由工作流作者声明业务预算

| 参数 | 默认值与输入域 | 作用范围与超限结果 | 处置 |
| :--- | :--- | :--- | :--- |
| `graph.max_steps` | `1000`；正整数 | 每个图帧独立计步；普通节点激活或 `for_each` 元素取得许可后计一步，空集合与节点内部重试不额外计步。超限取消并排空在途任务；根图为 `MaxStepsExceededError`，子图向父节点报告 `max_steps`。 | 保留业务护栏，不与历史事件数合并。 |
| `timeout_ms` | 可选正整数；省略时的行为见第三节 | 根 `run`、图内 `run`、内联 `graph`、`use: workflow:*` 的单次尝试；`builtin:*` 禁用。 | 保留；Activity 两侧期限使用同一预算。 |
| `retry.max_attempts`、`retry.backoff_ms`、`retry.on` | 默认无重试；启用时总尝试数 `>=2`，退避 `>=0` 且默认 `0`；普通节点默认选择 `[execution, timeout]`，signal 仅 `[timeout]` | 同一次节点或元素激活拥有一份总尝试预算；重用输入快照，失败尝试丢弃局部状态。 | 保留；原生重试与解释器重试是实现选择，不增加第二套重试配置。 |
| `for_each.concurrency` | `1`；正整数 | 同一集合激活内的并发迭代许可，覆盖元素的等待、重试与清理。 | 保留业务并发；不等同于 Worker 槽位或集合大小上限。 |

精确计步、回滚与异常选择器复用[运行护栏](../manual/spec/04-runtime-execution-model/03-guardrails-and-step-accounting.md)和[异常处理规范](../manual/spec/04-runtime-execution-model/04-exception-handling-and-unwinding.md)，不在此复制状态机。

### 2.2 执行请求：运行选项与后端原生属性

入口为 `ExecutionService(..., options=...)` 或直接提交的 `request["options"]`。生产宿主默认提交 `{}`；Workflow 补齐下列六个键，拒绝未知键，向 Child Workflow 传递完整结果。

| 选项 | 默认值与输入域 | 作用范围与相互关系 | 处置 |
| :--- | :--- | :--- | :--- |
| `activity_timeout_ms` | `604800000`（7 天）；正整数 | Activity 型 run 省略 `timeout_ms` 时的单次尝试预算，不约束排队、复合节点或 signal 等待。 | 保留宿主兜底；显式节点期限优先。 |
| `heartbeat_timeout_ms`（T） | **调整为 `30000`**；正整数，最终满足 `I < T` | 请求的 Worker 失活检测窗口；Server 实际值不超过 Activity 的 `start_to_close_timeout`。 | 保留；不默认关闭心跳超时。 |
| `heartbeat_interval_ms`（I） | **省略时派生为 `max(1, T // 3)`**，生产默认 `10000`；正整数且 `I < T` | Activity 本地调用心跳的间隔，不是实际 RPC 周期。 | 自动派生默认值，保留交互场景的显式覆盖。 |
| `max_call_depth` | `64`；正整数 | 根深度为 `1`；进入内联图或工作流调用加一，迭代诊断帧不加深；跨 Child 继续传递逻辑深度，超限为 `StackOverflowError`。 | 保留独立护栏；不与源码嵌套深度合并。 |
| `child_workflow_history_events` | `None` 或正整数 | 在 `use: workflow:*` 边界，SDK 建议或显式阈值触发 Child 执行。它不会截断父历史，不是历史总量硬上限。 | 保留切分提示；生产默认采用 SDK 建议，测试可注入阈值。 |
| `task_queues` | `{}`；字符串键、非空字符串值的映射 | 按 executor 名称路由 Activity；未命中时沿用当前 Workflow 队列。路由不改变业务并发许可。 | 保留部署路由；不新增 DSL 队列字段。 |

定义入口：[Workflow 选项与调度](../python/axcutor_runtime/src/axcutor_runtime/workflow.py)。

后端原生启动属性同样属于执行元数据，但不加入上述六键 options：

| 机制 | 当前采用值 | 生命周期与处置 |
| :--- | :--- | :--- |
| Workflow `task_timeout` | 入口不显式设置，Server 默认 `10s` | Worker 领取一个 Workflow Task 后的执行期限；失败后由 Server 重试该任务，不等于业务节点超时。保留后端默认，不新增 Axcutor 配置项。 |
| Workflow `execution_timeout` / `run_timeout` | 入口不设置整体执行或单 Run 期限 | 不给无限期 signal 等待另加总体期限；它们与节点 `timeout_ms`、Activity 尝试预算分别归属。保留当前默认。 |
| Workflow / Child 的 `RetryPolicy` | `maximum_attempts=1` | 不自动重启整个失败 Run；与 DSL 节点重试和 Workflow Task 自动重试分别计量。保留现有策略。 |

Workflow Task 默认期限与重试说明见 [Temporal Workflow 超时](https://docs.temporal.io/encyclopedia/detecting-workflow-failures)、[重试边界](https://docs.temporal.io/encyclopedia/retry-policies)。

### 2.3 宿主、Worker 与部署：由进程创建方配置

Worker 统一通过 `create_worker(..., **kwargs)` 透传 SDK 配置，不将以下参数再映射成 DSL、HTTP 请求字段或额外 CLI 开关。SDK 数值基线依据锁定的 [Temporal Python SDK 1.35.0](https://github.com/temporalio/sdk-python/blob/1.35.0/temporalio/worker/_worker.py)。

| 参数或机制 | 默认值 / 当前部署值 | 所有权与生命周期 | 处置 |
| :--- | :--- | :--- | :--- |
| `ExecutionService.rpc_timeout` | `5s`；正的 `timedelta` | 宿主控制面操作预算；与业务执行期限无关，具体边界见第三节。 | 保留构造参数，不进入重放状态。 |
| `max_heartbeat_throttle_interval`、`default_heartbeat_throttle_interval` | SDK 为 `60s / 30s`；当前两者硬编码为 `100ms` | Worker 控制心跳 RPC 合并；不能自行生成应用心跳。 | **删除硬编码，恢复 SDK 默认并允许透传覆盖。** |
| `max_concurrent_activities`、`max_concurrent_workflow_tasks` / `tuner` | SDK 参数为 `None`；未配置时固定槽位各 `100` | 当前 Worker 的 Activity / Workflow Task 执行资源；与 tuner 的互斥约束交由 SDK 校验。 | 沿用 SDK，不新增 Axcutor 全局并发参数。 |
| `max_cached_workflows` | SDK 为 `1000` | Worker 缓存容量，不是运行中 Workflow 数量上限。 | 沿用 SDK。 |
| `workflow_task_poller_behavior`、`activity_task_poller_behavior` | SDK 默认最大 poller 数各 `5` | Worker 拉取任务的并发，不等于业务并发或任务槽位。 | 沿用 SDK。 |
| `max_activities_per_second`、`max_task_queue_activities_per_second` | SDK 为 `None` | 分别限制 Worker 与队列的 Activity 启动速率；排队不计入尝试预算。 | 沿用 SDK；不派生为 DSL 限额。 |
| `sticky_queue_schedule_to_start_timeout` | SDK 为 `10s` | Workflow sticky 队列的调度回退，与业务 Activity 排队期限无关。 | 沿用 SDK，不复用 `activity_timeout_ms`。 |
| `graceful_shutdown_timeout` | SDK 为 `0s`；CLI Worker 显式 `20s`，Playground Worker 显式 `5s` | Worker 停机时，向在途 Activity 发起本地取消前的宽限期；后续仍等待其退出。 | 保留宿主覆盖，不当作清理完成的硬期限。 |
| Uvicorn `timeout_keep_alive` | `5s` | HTTP 空闲连接保活，不是活跃请求、请求体接收或业务执行期限。 | 沿用 HTTP 宿主默认。 |
| Uvicorn `limit_concurrency`、`limit_max_requests`、`backlog` | `None`、`None`、`2048` | HTTP 准入、进程请求计数与监听队列容量，与图内并发和 Activity 槽位无关。 | 沿用 HTTP 宿主默认，不新增业务限额。 |
| Uvicorn `timeout_graceful_shutdown` | `None` | 应用 lifespan shutdown 前的连接与请求排空，不受 `Playground.close()` 中各阶段预算约束。 | 保留当前不限时策略，明确接受其可能等到容器强制退出的边界。 |
| Compose `stop_grace_period` | api / worker 为 `30s`；playground 为 `35s` | 部署层在正常停止后等待进程退出，届满可强制终止进程。 | 保留部署配置，与应用关闭阶段一起核对。 |
| Temporal 容器健康检查 | interval `5s`、timeout `5s`、retries `12`、start_period `10s` | Compose 的依赖就绪判断，不负责判断 Activity 是否存活。 | 保留部署配置，不与心跳 timeout 合并。 |

定义入口：[宿主服务](../python/axcutor_host/src/axcutor_host/service.py)、[CLI Worker](../python/axcutor_host/src/axcutor_host/cli.py)、[Worker 组装](../python/axcutor_runtime/src/axcutor_runtime/worker.py)、[Compose](../compose.yaml)。

HTTP 参数由服务启动方通过 Uvicorn Config 管理；本仓入口沿用锁定的 [Uvicorn 0.54.0 默认值](https://github.com/Kludex/uvicorn/blob/0.54.0/uvicorn/config.py)，不将它们包装成新的 DSL 或业务请求参数。

### 2.4 模块内部固定机制：不增加面向用户的配置

| 机制 | 当前值 | 生命周期与设计决定 |
| :--- | :--- | :--- |
| 源码 / 模型 JSON 深度保护 | `MAX_SOURCE_DEPTH=128`；`MAX_MODEL_DEPTH=128+4` | 编译器输入边界防止过深递归；模型余量容纳分析结果的封装层。保留派生关系，不机械统一两种载荷深度。 |
| 浏览器无损 JSON 深度保护 | `512` | 编辑器解析保护；不扩大编译器允许的源码深度。保留前端私有常量。 |
| 大整数表示阈值 | 解析 token 长度 `4000`；编码 bit length `>13000`；Literal 生成使用 `<13000` | 超阈值改用 RawJSON 等表示或跳过 Literal，不是业务整数的大小上限。保留内部策略，不暴露 `max_integer`。 |
| signal 初始化 Timer | 名义 `1ms`；每个 Workflow Run 至多一次 | 建立持久化调度边界，不阻塞信号投递，也不构成业务等待或响应延迟承诺。保留内部机制。 |
| Workflow activation 死锁检测 | SDK 非 debug 模式为 `2s` | Worker 的本地执行保护；触发 Workflow Task 失败，不直接转为 DSL `timeout` 或 Workflow 终态。保留 SDK 机制，不新增超时字段。 |
| Playground 准入与解锁 | 单实例一个根执行 | 确定性预检拒绝不占用执行 ID；已提交或结果未知时保留 `execution_id`，直到 Temporal 终态且本地执行器排空。暂时 `NotFound` 不授权解锁。保留现有所有权，不增加业务并发字段。 |
| Playground 完成观察重试 | 后端暂不可用或执行暂不可见时等待 `1s` | 只重新观察原执行，不授权重新提交或提前解锁。保留内部间隔。 |
| Playground 关闭阶段 | 取消调用预算 `5s`，观察完成预算 `10s` | 两个阶段有不同超时动作，见第三节；不另设总清理 timeout。 |
| 前端状态轮询 | 执行 `300ms`；工作区 busy / idle 为 `300ms / 1000ms` | 浏览器视图的刷新频率，不能作为执行终态、取消送达或后端失活判据。保留模块私有间隔。 |

定义入口：[源码解析](../crates/axcutor-core/src/source.rs)、[无损编解码](../python/axcutor_runtime/src/axcutor_runtime/codec.py)、[契约生成](../python/axcutor_runtime/src/axcutor_runtime/builtin.py)、[前端 JSON](../playground/src/utils/losslessJson.ts)、[Playground 协调器](../playground/backend/src/axcutor_playground/coordinator.py)、[执行轮询](../playground/src/composables/useExecution.ts)、[工作区轮询](../playground/src/composables/useWorkspace.ts)。signal Timer 的完整契约见[执行器 ABI](../manual/spec/05-executor-abi/01-run-executor-contract.md)。

本地死锁检测的基线见 [SDK Workflow Worker](https://github.com/temporalio/sdk-python/blob/1.35.0/temporalio/worker/_workflow.py)。

## 三、跨机制约束与生命周期

### 3.1 排队、尝试与整个激活

Activity 的有效尝试预算 A 为节点显式 `timeout_ms`，省略时取 `activity_timeout_ms`。Server 的 `start_to_close_timeout` 与 Activity 本地 deadline 均从 A 派生；前者从 Worker 领取后计时，后者在本地尝试入口建立，不追求两个时钟完全重合。

Activity 排队不消耗 A；本方案不新增 `schedule_to_start_timeout` 或 `schedule_to_close_timeout`，允许等待离线 Worker。无人消费的队列可以持续等待，7 天兜底和心跳都不能限制它。需要限制这一段的业务，可在已有复合节点上配置 `timeout_ms`，该节点的 Workflow 定时器覆盖其后代等待。

复合节点省略期限时不设限；signal 省略期限时无限等待，不采用 Activity 兜底，也不占 Activity 槽位。重试的每次尝试重置期限，`backoff_ms` 不计入下一次尝试；外层复合节点的期限则覆盖其内部尝试、退避与等待。超时触发取消后仍需排空任务，因此期限不是 `catch` 或重试开始时间的硬上界。

例如，内联图的 `timeout_ms: 1000` 到期会取消后代；若后代是期限较长的 Activity，该图仍等待其收到取消并清理后才进入后续分支。祖先期限不会自动改写 Activity 的本地 deadline 或心跳间隔。

### 3.2 并发、步数与历史资源

`for_each.concurrency` 限制一个集合激活内的迭代，许可覆盖其内部重试、signal 等待与退出清理；Worker 槽位限制该进程实际执行的任务，poller 只负责拉取。多张图、多次集合激活和多个 Worker 各有自己的限额，不能将这些数值解释为全系统统一上限。

`max_steps` 限制一个图帧的调度次数，`max_call_depth` 限制逻辑调用路径，历史提示限制的是调用边界的执行位置选择。三者不互相替代；切换到 Child 不重置逻辑深度，也不删除父历史。没有工作流调用边界的长循环仍受 Temporal 历史限制，本提案不声称提供历史总量硬护栏。

Activity 型 run 声明的 `retry`，仅在包含 `timeout`、不包含 `contract`、`backoff_ms > 0` 且满足后端下推范围时使用原生重试，固定退避系数为 `1.0`；当前实现的范围是 `max_attempts <= 2147483647`、`backoff_ms <= 2147483647000`，其余情况使用 Workflow 循环。这些是下推阈值，不是整个 DSL 的统一整数上界。默认 `backoff_ms: 0` 必须保持立即重试；`contract` 还可能产生于 Workflow 二次校验，因此也由 Workflow 统一管理总尝试预算。不新增 `native_retry` 开关，也不静默截短参数。

Workflow Task 的 Server 超时与 Worker 死锁检测保护解释器自身，不能替代上述业务护栏。即使 `max_steps`、调用深度均未超限，较慢的同步 builtin 或重放也可能触发它们；失败的是 Workflow Task，Server 可持续重试，执行仍可能保持 running。这类重试不受 DSL `max_attempts` 或整个 Run 的 `maximum_attempts=1` 限制。

### 3.3 心跳、尝试期限与取消

先做统一正整数校验，再为省略的 I 计算 `max(1, T // 3)`，最后校验 `I < T`。只有键缺失时才派生，显式 `None` 仍为非法类型。显式 I 原样保留；只设置 I 时 T 仍为 `30000`。例如 `T=1000` 推导 `I=333`，`T=120000` 推导 `I=40000`；`T=2` 推导 `I=1`，`T=1` 因不满足最终不变量被拒绝。

Server 的有效心跳 timeout 为 `min(T, A)`；SDK 按有效 timeout 的 `0.8` 倍与 Worker 最大节流间隔的较小值合并心跳。只有应用持续调用心跳，SDK 才有待发送内容；它不会因设置了节流参数而自动生成心跳，见[Temporal 心跳说明](https://docs.temporal.io/encyclopedia/detecting-activity-failures#throttling)。

| 场景 | 请求选项 | Worker 节流 | 取舍 |
| :--- | :--- | :--- | :--- |
| 生产默认 | `{}`，得到 `T=30000, I=10000` | SDK 默认 | 长尝试典型节流窗口约 `24s`；降低 RPC 频率，同时接受数十秒量级的取消送达等待。 |
| Playground | `options={"heartbeat_interval_ms": 200}`，保留默认 T | 显式 `max_heartbeat_throttle_interval=200ms` | 同时缩短两处间隔，在不缩短失活检测窗口的前提下增加取消送达机会。 |
| 取消测试或其他交互宿主 | 按场景显式覆盖 | 与应用间隔成对配置 | 测试加速值留在夹具，不提升为生产默认或 DSL 字段。 |

保留 I 的可选覆盖是为了解耦失活检测与取消响应。只调整应用间隔或 SDK 节流中的一个不足以获得快速取消；短于 I 的尝试可能仅发送首次心跳，仍由自身期限结束，不另加按节点期限缩放心跳的规则。生产慢取消同样影响并发失败和复合节点超时后的任务排空。

Activity 继续使用 `WAIT_CANCELLATION_COMPLETED`：心跳送达 Server 取消；本地 deadline 在预算到期且事件循环可调度时置位 `AbortSignal`，原因是 `"timeout"`；提交前的单调时钟复查拦截同步序列化或契约校验造成的迟到成功。Server 超时与本地超时统一映射为 `timeout`。退出时由 `cancel_and_drain` 等待拥有的任务，不能以 SDK 或 Server 的通用取消替代这些职责。

本地排空不提供跨进程、跨尝试互斥，Server 超时后可能重试；外部副作用仍由执行器与外部系统保证幂等。清理耗时也不由心跳间隔保证。

### 3.4 控制面 RPC、观察与停机

`rpc_timeout` 约束一次后端调用及其连接、重试等待；同一公开方法可能串行调用多个 RPC，故 `5s` 不是整个 HTTP 请求的统一总预算。signal 在根执行检查后，对跨 Child 寻址和 Update 投递另用同一个 `rpc_timeout` 约束整段操作；后续视图读取仍遵循单次 RPC 预算。

`ExecutionService.wait()` 中间的执行结果等待有意不设业务期限；前后的状态读取仍有 RPC 预算。观察调用超时或被取消，不等于业务执行终止；提交或信号投递后收到 `503 runtime_unavailable` 可能表示结果未知，调用方应查询原执行，不能据此重复提交。

HTTP 宿主先停止接收新连接，排空现有连接和请求，再发送 lifespan shutdown，之后才进入 Playground 的应用关闭。当前 `timeout_graceful_shutdown=None`，因此挂起请求可能耗尽容器的 `35s`，使 `Playground.close()` 尚未开始；本次保留这一部署取舍，不将其误算进后续 `5s / 10s`。若宿主显式给 HTTP 排空有限预算，届满由 Uvicorn 取消剩余请求任务后进入 lifespan shutdown，见[关闭顺序](https://github.com/Kludex/uvicorn/blob/0.54.0/uvicorn/server.py)。

进入应用关闭后按以下所有权顺序推进：

1. Playground 给取消调用 `5s`，随后给完成观察 `10s`；观察超时只结束该等待，不直接宣告执行结束或释放工作区。
2. 退出 Worker：CLI 的 `20s`、Playground 的 `5s` 是本地取消前的宽限期，届满后发起本地取消并继续等待在途任务退出。这条本地取消路径不必等待 Server 心跳响应。
3. Worker 完成退出后再卸载模块、释放工作区。运行期间只有 Temporal 终态与本地 `_drained` 同时满足，才能清除 `execution_id` 并允许下一次执行。
4. Compose 的 `30s / 35s` 是进程外停止期限，可能强制中断仍未完成的清理。应为显式关闭阶段留余量，但不能用 `5+10+5<35` 证明业务一定清理完毕；合作式清理本身没有硬时长保证。

Playground 的快速心跳为 `10s` 完成观察阶段留出清理机会；不保证终态、解锁或停机必定在该窗口内完成。容器健康检查、浏览器轮询与观察失败重试均不参与业务超时判定。

## 四、校验、持久化与演进

业务 JSON 的任意精度数值承诺不表示任意大的控制参数都能由当前后端执行。控制整数必须保持消费处要求的整数表示，时间还须能转换为对应时长并满足 SDK 字段约束。不可表示的值由实际消费边界按既有错误映射处理，无法构造执行参数时为不可恢复 `EngineError`，不当作无限等待或静默截短；不得因收紧入口检查，提前改变旧历史中尚未使用这些选项的执行控制流。

编译器保留现有 DSL 整数规则，运行时保留现有入口的形状、正整数和参数关系校验；不为后端增加一轮整包 AST 时间扫描，也不将不同用途的控制参数统一截成任意的固定上界。SDK 不支持的重试下推参数按第三节选择 Workflow 路径，其他不可表示值由实际消费边界处理。

宿主构造时复用运行选项的入口规则，非法类型、未知键或参数关系报 `ValueError`，但保存的仍是原始覆盖字典，不保存校验过程补齐的结果。直接 Workflow 请求继续在入口校验，错误按既有规则成为不可恢复 `EngineError`。根请求据此持久化覆盖值，缺省值在执行或重放时补齐；Child 请求携带父 Run 已展开的完整选项，重复解析不得改写显式值。进程级配额、前端间隔与部署时序不写入执行请求，也不在重放中读取。

本次心跳默认值直接切换，不引入 `workflow.patched`：对旧版本已通过校验并进入执行的历史，已有 Activity 的请求和 Server 原生重试保留历史参数，重放后的新调度使用补齐的新默认值，已启动的 Child 保留完整旧值。Temporal 允许修改 Activity / Child 的输入参数与超时；这一兼容结论仅适用于本次心跳变更，见[重放约束](https://docs.temporal.io/workflow-definition#deterministic-constraints)。未来修改步数、深度或历史切分规则等控制流行为时，须另行验证旧历史，不能套用此结论。

## 五、实施与验证边界

本提案的结果包括完整矩阵、跨机制约束及其处置，不要求为每个保留项重写实现。代码变更集中在心跳默认值与派生、Worker 节流透传及 Playground 成对覆盖、宿主运行选项的早期结构校验与原始覆盖值保留。其余机制按矩阵保留原有入口，不创建统一计时器、全局配额对象、新协议或额外的时间预检阶段。

验证应覆盖：四类入口的配置归属与既有拒绝规则；尝试重置、退避与外层期限的关系；业务并发与 Worker 资源的独立性；超时后清理和迟到结果丢弃；控制面超时不取消业务；确定性提交拒绝不遗留 busy，结果未知时不提前释放所有权；停机与工作区排空顺序；旧版缺省配置历史重放及 Child 选项继承。测试专用快速参数由夹具显式注入，并单独保留生产缺省路径的验证。

后续新增任何预算或护栏，均在对应类别中回答“约束什么、谁配置、何时开始和结束、超限后做什么、如何与已有机制配合”。不能只因名称都带 `max_` 或 timeout 就合并，也不能因保持内部常量就从系统性盘点中省略。
