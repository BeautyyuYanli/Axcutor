# RFC-0017: 协作式挂起执行器 `signal`、Temporal Update 恢复协议与宿主 `:signal` 接口规范

* **状态**：Implemented
* **所属分类**：执行器 ABI 扩展、Temporal 确定性挂起调度与宿主 HTTP/CLI 恢复协议（Cooperative Signal Executor, Workflow Update Suspension & Host Signal API）
* **前置关联提案**：
  * [`RFC-0011: Axcutor 编译器前后端与 Temporal 运行时参考实现架构`](./0011-compiler-and-temporal-runtime-architecture.md)
  * [`RFC-0012: 结构化 run 执行器分派机制、移除自然语言变量正则提取与 builtin 可信 Python 执行器规范`](./0012-run-executor-and-builtin-python.md)
  * [`RFC-0014: 基于预置 Python 模块引用的 module 执行器、__axcutor__.yaml 包同构布局与契约模板 SHA 校验规范`](./0014-module-python-executor.md)
* **核心解决的问题**：
  1. **新增纯等待内置执行器 `signal`（`run: { executor: "signal" }`）**：在不引入节点内 Python 栈序列化、重放缓存（Replay Journal）或长周期 Activity 进程占用的前提下，将“挂起等待外部消息输入”建模为独立的原子 `run` 执行器，挂起前后的外部通知或后置处理交由控制流图（CFG）中前后相邻的 `builtin` / `module` 节点完成；
  2. **基于 Temporal Workflow `wait_condition`、`@workflow.update` 与 `@deliver_signal.validator` 实现零 Activity 开销的确定性挂起与唤醒**：`signal` 节点在 `AxcutorInterpreterWorkflow` 协程内直接挂起，不发起 Temporal Activity、不占用 Worker 槽位、不产生心跳流量；通过 `@deliver_signal.validator` 在只读阶段同步执行挂起地址核验、可选步数 CAS 核验与 `axcutor_core.validate_run_contract` 契约校验，拒绝请求零 Event History 写入；
  3. **确立“契约校验失败视为非法信号（`422`）而非节点 `contract` 异常”的边界隔离准则**：外部提交的信号若不符合目标挂起节点的 `(output, branches)` 契约，直接在 Update Validator / HTTP 边界拒绝并返回 `422 signal_validation_failed`，挂起节点继续保持等待状态；同时在编译期收紧 `signal` 节点的可恢复异常空间为纯 `{"timeout"}`（禁止显式声明永不触发的 `execution` 或 `contract` 异常选择器）；
  4. **扩展宿主服务、HTTP API（`POST /v1/executions/{execution_id}:signal`）与 CLI（`axcutor signal`）**：支持按节点运行时拓扑地址 `address`（及可选乐观锁步数 `step`，自动递归穿透 `child_execution` 派生的多层 Temporal Child Workflow）精确投递信号，并在 `Frame` 视图中无歧义地暴露实时挂起等待地址列表 `waiting: list[str]`。

---

## 一、 问题背景与架构权衡

### 问题 17.1：长周期人机协同与外部异步回调需要零进程占用的原生挂起机制

在实际业务工作流（如人工审批、外部 Webhook 回调、跨系统异步确认、多轮 Agent 交互）中，工作流需要在某个控制流节点暂停数分钟乃至数天，直到收到外部消息后再携带该消息提供的数据与分支选择恢复运行。

若试图通过现有的 `builtin` 或 `module` Python Activity 执行器实现长周期等待，或试图在单段 Python 函数中间通过 `yield` 多轮挂起并恢复局部变量栈，会面临以下架构冲突：

1. **Activity Worker 槽位占用与心跳耦合**：`RunActivities.execute_run` 绑定了强心跳存活探测（`heartbeat_interval_ms` / `heartbeat_timeout_ms`）。在 Activity 内阻塞等待外部事件会长期占用 Worker 并发配额并持续产生心跳 RPC；一旦 Worker 重启，Activity 尝试会因心跳超时而重头执行，导致挂起点之前的外部副作用重复触发。
2. **沙箱外 Python 局部栈不可持久化**：`builtin` 与 `module` 运行在非确定性的普通 Python 进程中。若要在同一个 `async def execute` 函数内挂起并在新进程中从原位置继续执行下半段，就必须在 Temporal 之上额外发明一套节点级 `state` 检查点或副作用重放日志（Replay Memoization），这与 Temporal 自身的确定性工作流状态机职责重叠，属于过度工程化。
3. **Axcutor 的 CFG 图本身就是天然的多阶段状态机**：在 Axcutor 中，`Frame.env` 已经由 Temporal Event History 自动持久化。将“挂起前的准备/通知”、“挂起等待外部信号”、“收到信号后的处理”拆分为 CFG 图上的相邻节点，即可 100% 复用现有的变量作用域、分支路由（`branches`）、超时护栏（`timeout_ms`）与并发屏障（`for_each` / `fork_join`），无需发明任何新的类型系统或状态恢复原语。

### 问题 17.2：外部信号输入属于“不可信外部请求”而非“内部执行器契约违约”

在 `builtin` 与 `module` 执行器中，返回值由工作流内部受控代码产出；当返回值违反 `output` / `branches` 契约时，属于内部实现缺陷或模型输出格式错误，因此映射为进入节点 `retry` / `catch` 状态机的 `contract` 异常。

然而，对于等待外部消息的 `signal` 节点，信号载荷由外部调用方通过 HTTP/CLI 提交：
* 若外部调用方传错了字段类型、漏填了必填字段或指定了不存在的分支，这属于**当次外部投递请求非法（Client Input Error）**；
* 如果将其视为节点的 `contract` 异常，一次错误的外部请求就会直接中断或推进正在等待的工作流节点（甚至使整个工作流失败退出），导致合法信号无法再送达；
* 因此，信号契约校验必须在接收边界（Temporal `@deliver_signal.validator` 处理器）同步完成：**校验不通过时直接在 Validator 阶段向调用方报错拒绝（不写入 Workflow Event History），工作流挂起状态保持分毫无损**。

### 问题 17.3：单次 Workflow Run 的 Temporal Update 容量边界与多轮循环最佳实践

每次被 Validator 接受并完成的 `@workflow.update` 都会在当前 Temporal Workflow Run 的 Event History 中记录 `WorkflowExecutionUpdateAccepted` 与 `WorkflowExecutionUpdateCompleted` 事件。根据 Temporal 平台限制，单个 Workflow Run 最多支持同时 10 个在途 Update、历史累计最多 2,000 次 Update 事件（及 50,000 条总历史事件）。
* 由于 `@deliver_signal.validator` 拒绝的非法请求（`409` / `422`）在进入 History 前即被丢弃，外部高频错误请求不会消耗该配额；
* 对于正常唤醒的信号，单图内数十至数百轮的循环交互完全在单 Run 容量范围内；若业务需要单次执行内经历上千轮以上的超长周期 `signal` 循环，推荐将单轮或分段交互逻辑封装为独立的子工作流并通过 `use: workflow:<package_path>` 调用，从而自动利用运行时现有的 `is_continue_as_new_suggested()` / Child Workflow 派生机制卸载并重置历史事件长度，无需在单图内部引入复杂的跨挂起点 Continue-As-New 栈检查点机制。

---

## 二、 核心设计决策

### 决策 1：新增闭合内置执行器 `RunSpec::Signal`（`run: { executor: "signal" }`）

1. **`RunSpec` 文法扩展与互斥约束**：
   在 [`01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md) 的 `RunSpec` 标记联合体中新增内置分支 `SignalRunSpec`，并从开放扩展分支 `ExtensibleExecutorRunSpec` 的判别式中排除 `"signal"`，确保联合体分支严格互斥：
   ```txt
   RunSpec                   ::= BuiltinRunSpec
                               | ModuleRunSpec
                               | SignalRunSpec
                               | ExtensibleExecutorRunSpec ;

   SignalRunSpec             ::= {
     executor: "signal"
   } ;

   ExtensibleExecutorRunSpec ::= {
     executor: Identifier \ {"builtin", "module", "signal"},
     ...
   } ;
   ```
   * **闭合性约束**：当 `executor == "signal"` 时，`run` 映射对象必须且只能包含 `executor: "signal"` 一个键（无需也不允许声明 `content` 或任何其他字段）；编译器 `schema::run()` 在进入扩展执行器 fallback 前优先匹配 `"signal"` 并执行 `closed(m, &["executor"], a, ds)`，若包含任何额外字段，编译期 Pass 1 在 `/run/<field>` 处报 `schema.unknown_field` 错误。
   * **AST 表示与序列化（`crates/axcutor-core/src/ast/mod.rs`）**：
     ```rust
     pub enum RunSpec {
         Builtin {
             content: String,
         },
         Module {
             content: String,
         },
         Signal,
         Extension {
             executor: String,
             config: Ordered<Value>,
         },
     }
     ```
     在 `RunSpec` 的手写 `Serialize` 实现中，`RunSpec::Signal` 序列化为单键映射 `{"executor": "signal"}`。
2. **与现有节点修饰符及步数统计的正交配合**：
   * **`input`、`output`、`branches`**：
     * `input` 声明该 `signal` 节点在挂起前必须已定值的只读依赖变量（参与编译期 Pass 5 必达定值分析；运行期外部可直接从所属 `Frame.variables` 读取挂起上下文变量）；
     * `output` 与 `branches` 直接定义外部信号恢复该节点时必须满足的静态契约（无需额外声明独立的 message schema）。
   * **`timeout_ms`**：
     * 若 `signal` 节点声明了 `timeout_ms`，表示该节点单次等待外部信号的最大允许时长（使用 Workflow 虚拟时钟 `_with_timeout` 计时，**不适用** `activity_timeout_ms` 默认 7 天限制；若未声明 `timeout_ms`，则无限期挂起直至收到信号或工作流被取消）。
     * 一旦在收到合法信号前触及 `timeout_ms`，立即取消当前挂起等待并产生标准可恢复异常 **`timeout`**，进入节点的 `retry` / `catch` 状态机。
   * **`retry` 与 `catch`（编译期异常选择器收紧为 `{"timeout"}`）**：
     * 由于 `signal` 节点不执行外部代码且非法信号在 Validator 边界直接返回 `422` 拒绝，运行期 `signal` 节点**仅可能产生 `timeout` 可恢复异常**，永远不可能产生 `execution` 或 `contract` 异常；
     * 为防止用户误以为外部传入错误信号会触发 `catch: { bad: [contract] }` 从而写出永远无法到达的死分支，编译期在 [`schema.rs`](../crates/axcutor-core/src/compiler/schema.rs) 中将 `signal` 节点的可恢复异常全集（Universe）收紧为单元素集合 **`{"timeout"}`**：
       1. 在 `signal` 节点上，若 `retry.on` 或 `catch.<branch>` 的显式选择器列表包含了 `"execution"`、`"contract"` 或 `"max_steps"`，编译期报 `schema.selectors`（`"invalid recoverable selector for this node"`）错误；
       2. 若 `signal` 节点声明了 `retry` 但省略了 `on`，其默认 `retry.on` 自动归一为 `["timeout"]`（而非普通 `run` 节点的 `["execution", "timeout"]`）；
       3. 若 `signal` 节点的 `catch` 使用了通配符 `"*"`，其求补全集为 `{"timeout"}`（即 `"*"` 自动展开为 `["timeout"]`；若同时已有另一个分支显式捕获了 `["timeout"]`，则 `"*"` 因展开为空集报 `schema.catch` `"wildcard catch expands to an empty set"` 错误）。
     * **禁止下推至 Activity Native Retry**：在 `AxcutorInterpreterWorkflow._state_machine` 中，定义 `is_activity = body["kind"] == "run" and body["run"]["executor"] != "signal"`。仅 `is_activity == True` 的节点允许启用 Temporal Activity 原生重试下推（`native = True`）；`signal` 节点始终走 Workflow 级重试循环，且当声明了 `timeout_ms` 时在 `_state_machine` 中对每次尝试统一包裹 `_with_timeout(operation, timeout)`。每次重试尝试复用同一 `input` 快照、重新注册等待槽位并重置超时计时器。
   * **步数护栏（`steps` / `max_steps`）**：
     * 严格遵循 [`03-guardrails-and-step-accounting.md`](../manual/spec/04-runtime-execution-model/03-guardrails-and-step-accounting.md) 现有计步规则：进入 `signal` 节点激活（或启动 `for_each` 迭代元素）时按常规计 1 步；挂起等待期间、Validator 拒绝非法信号以及节点内部 `timeout` 重试均**不额外递增** `Frame.steps`。
   * **`for_each` 与 `fork_join`**：
     * 支持在 `for_each` 或 `fork_join` 并发分支中使用 `signal` 节点（在 `fork_join` 中作为直接并发成员时仍须遵守现有 SESE 拓扑约束，即非 `for_each` 直接成员不可声明多分支或 `catch` 出口，复杂分支可置于内联子图中）；
     * 每个并发激活实例拥有唯一确定的运行时拓扑地址 `address`（如 `main/approve_items[0]`、`main/approve_legal`），外部可按 `address` 逐个独立投递信号；在 `for_each` 挂起等待期间，该迭代实例继续持有其 `concurrency` 并发信号量槽位；
     * 唤醒后的变量写回严格服从所在控制流结构的屏障语义：普通顺序节点立即将输出提交至 `Frame.env` 并推进 `frame.cursor`；`fork_join` 与 `for_each` 分支则将输出写入各自的并发槽位，待屏障内所有分支全部收敛后才原子合并回父 `Frame.env`（若同组其他并发分支失败触发 Fail-Fast 取消，则整体回滚，不污染父环境）。
   * **契约生成（`axcutor contracts`）**：
     * `generate_contracts` 仅针对 `executor == "module"` 生成 `.py` 契约模板，遇到 `executor == "signal"` 时自动跳过，不产生任何宿主文件。

---

### 决策 2：基于 Temporal Workflow `wait_condition`、`@workflow.update` 与 `@deliver_signal.validator` 的纯等待执行机制

1. **零 Activity 调度的挂起生命周期与超时竞态裁决**：
   在 `AxcutorInterpreterWorkflow._attempt`（[`workflow.py`](../python/axcutor_runtime/src/axcutor_runtime/workflow.py)）中，当 `body["kind"] == "run"` 且 `body["run"]["executor"] == "signal"` 时，解释器**不调用 `_run_activity`**，而是直接进入 Workflow 内存挂起流程 `_wait_signal(frame, node, address)`：
   * `_attempt` 入参中的 `address` 即为当前挂起激活实例的唯一规范拓扑地址：
     * 根工作流或被调用子工作流自身为 `RootRunNode` 时，`address` 为该 `FunctionFrame` 的 `frame.address`（如 `"__main__"` 或 `"main/invoke"`）；
     * `for_each` 的 `IterationFrame` 时，`address` 为该迭代帧地址（如 `"main/batch[0]"`）；
     * 图内普通 `ActionNode`（含顺序节点与 `fork_join` 并发成员）时，`address` 为 `f"{frame.address}/{node_id}"`（如 `"__main__/await_decision"`）。
   * 在 Workflow 实例的挂起注册表 `self._waiters: dict[str, _SignalWaiter]` 中，以 `address` 为键登记一个等待槽位 `_SignalWaiter`（记录所属栈帧 `frame` 引用以读取 `frame.id` 与当前步数 `frame.steps`）：
     ```python
     @dataclass
     class _SignalWaiter:
         frame: _Frame
         address: str
         output_schema_json: str
         branches_contract_json: str
         status: Literal["pending", "delivered", "expired", "cancelled"] = "pending"
         response: JsonObject | None = None
     ```
   * **单次尝试结果的唯一裁决（消除同轮次唤醒与计时器到期竞争）**：
     * 调用 `await workflow.wait_condition(lambda: waiter.status == "delivered")` 挂起当前协程；
     * 若节点声明了 `timeout_ms`，由于 `_with_timeout` 在同一 Workflow Turn 内当任务与定时器同时就绪时优先检查 `timer.done()`，为防止“Update 已在同一轮次认领 `waiter` 并向调用方返回成功，但外层 `_with_timeout` 因定时器同轮次就绪而将其改判为 `timeout`”的竞态：
       * 当 `_with_timeout` 的定时器触发取消 `task` 时，若 `waiter.status == "delivered"`（即 Update 已经在定时器生效前同步完成了认领），`_wait_signal` 捕获 `CancelledError` 后直接返回已认领的 `waiter.response`，不再抛出 `timeout`；
       * 反之，若定时器触发时 `waiter.status == "pending"`，则在 `finally` 块中将其标记为 `waiter.status = "expired"` 并立即从 `self._waiters` 移除，后续到达的 Update 因查无此 `waiter` 而返回 `SignalNotWaitingError`（`409`）。
     * 在 `finally` 块中，若 `self._waiters.get(address) is waiter`，无条件将其从 `self._waiters` 移除，确保超时、并发 Fail-Fast 取消或正常唤醒后绝不残留僵尸等待槽位。

2. **无损 JSON 传输契约与 `@deliver_signal.validator` / `@workflow.update` 唤醒协议**：
   为严格遵守 Axcutor 全局的“JSON text is the Temporal payload”无损编解码准则（保留任意精度十进制数 `Decimal`、大整数 `RawJSON` 与对象键序，见 [`codec.py`](../python/axcutor_runtime/src/axcutor_runtime/codec.py)），跨 Temporal Update 边界的入参与返回值统一采用 `codec.dumps` / `codec.loads` 编码的 JSON 字符串 `str`：
   * 在 `AxcutorInterpreterWorkflow` 上声明 `@workflow.update` 方法 `deliver_signal(self, request_json: str) -> str` 及其只读校验器 `@deliver_signal.validator`：
   * `request_json` 解码后的对象结构为 `{"address": str, "branch": str | None, "output": JsonObject, "step": int | None}`；
   * **阶段 A：只读验证阶段（`@deliver_signal.validator`——零 Event History 污染）**：
     由于未声明 Validator 的 Update 会先由 Temporal 写入 `WorkflowExecutionUpdateAccepted` 历史事件再执行 Handler，若将地址核验与契约校验仅放在 Handler 内，高频错误请求会直接膨胀 Workflow Event History 并消耗单执行 Update 配额。因此，所有拒绝判定均在只读同步函数 `_validate_signal_request(self, request_json: str) -> tuple[_SignalWaiter, str]` 中完成，并由 `@deliver_signal.validator` 调用：
     * **步骤 1（请求结构、挂起状态与可选 `step` CAS 核验）**：
       * 使用 `codec.loads(request_json)` 解码请求并校验顶层字段类型（捕获 `ValueError`、`TypeError`、`RecursionError`、`OverflowError` 并转为 `SignalValidationError`）；
       * 检查 `address` 是否存在于当前 `self._waiters` 中且 `waiter.status == "pending"`；若不存在（目标节点尚未到达、已完成、已超时或地址拼写错误），抛出 `exceptions.ApplicationError(f"no signal node waiting at {address!r}", type="SignalNotWaitingError", non_retryable=True)`；
       * **可选步数乐观锁（`step` CAS）核验**：若请求中提供了非空整数 `step`（`request.get("step") is not None`），核对目标挂起槽位所属栈帧的当前步数是否满足 `waiter.frame.steps == step`。若不匹配（例如在循环图中工作流已被上一轮信号唤醒并进入了新一轮同地址等待），立即抛出 `exceptions.ApplicationError(f"signal step mismatch at {address!r}: expected {step}, current {waiter.frame.steps}", type="SignalNotWaitingError", non_retryable=True)`（映射为 HTTP `409 Conflict`），从而在有向环路中彻底防止跨轮次迟到/重发信号误唤醒；
     * **步骤 2（契约边界同步校验——拒绝非法信号且不污染工作流状态）**：
       * 构造候选响应 JSON 字符串 `raw_response_json = dumps({"branch": request.get("branch"), "output": request["output"]})`，交由 `axcutor_core.validate_run_contract(raw_response_json, waiter.output_schema_json, waiter.branches_contract_json)`（三个参数均为 JSON `str`）执行严格校验（含分支合法性、必填输出字段完备性、深层 `TypeSpec` Schema 校验与顶层多余字段白名单过滤）；
       * 若抛出 `axcutor_core.ContractError`（或数值越界等 `ValueError` / `OverflowError`），立即抛出 `exceptions.ApplicationError(str(error), type="SignalValidationError", non_retryable=True)`；若抛出内核 `RuntimeError`，转为 `exceptions.ApplicationError(str(error), type="EngineError", non_retryable=True)`（防止未包装的原生 Python 异常导致 Workflow Task 卡死重试）；
       * **关键不变量**：Validator 阶段不修改 `waiter` 或 `Frame` 的任何字段；一旦 Validator 抛出异常，Temporal 直接拒绝该 Update，**不写入任何 Workflow Event History 事件**，`wait_condition` 不会被唤醒，节点也不会触发 `contract` 异常或消耗 `retry` 次数；
   * **阶段 B：原子认领与唤醒阶段（`@workflow.update def deliver_signal`）**：
     * Handler 再次调用 `waiter, validated_json = self._validate_signal_request(request_json)`（确保重放确定性，不依赖 Validator 留下的临时内存缓存）；
     * 将规范化后的结果赋给 `waiter.response = loads(validated_json)`，设置 `waiter.status = "delivered"`，并从 `self._waiters` 中弹出该 `waiter`；
     * `workflow.wait_condition` 在当前 Workflow Task 中就绪唤醒，`_wait_signal` 返回 `waiter.response`，`deliver_signal` 向宿主返回确认字符串 `validated_json`。

---

### 决策 3：运行时快照（`Frame`）可观测性与 Child Workflow 透明穿透

1. **`Frame` 挂起状态无歧义暴露**：
   在现有运行时中，`fork_join` 的多个并发分支共享同一个父图 `Frame`（且在分支执行期间 `frame.cursor` 停留在分叉前的前驱节点上）；同时，被 `use: workflow:...` 调用的 `RootRunNode` 子工作流帧（`cursor == "run"`，挂起地址为 `frame.address`）与停留在名为 `"run"` 的图内节点上的 `RootGraphNode` 帧（`cursor == "run"`，挂起地址为 `f"{frame.address}/run"`）拥有完全相同的 `kind` 与 `cursor`。
   因此，仅靠布尔值 `waiting: bool` 加 `frame.cursor` 无法在 `fork_join` 同帧多挂起点或 `cursor == "run"` 场景下无歧义地推导出目标 `address`。
   * 在 `Frame` 结构上新增字段 **`waiting: list[str]`**（按注册顺序排列的当前帧直接管辖的挂起等待规范 `address` 列表；未挂起时为空列表 `[]`）：
     * 在 `get_runtime_snapshot` 中，`frame["waiting"]` 直接由挂起注册表动态派生：`[w.address for w in self._waiters.values() if w.frame.id == frame.id and w.status == "pending"]`；
     * 当 `ExecutionService._frames` 将子工作流快照合并回父工作流树时，对子工作流中每一帧的 `waiting` 列表同步执行与 `frame["address"]` 相同的逻辑地址前缀替换：
       `waiting = [prefix + addr.removeprefix(child_entry) for addr in raw_frame["waiting"]]`；
     * 外部调用方与 Web Playground 直接读取 `frame["waiting"]` 中的字符串元素即可获得可直接传给 `:signal` 接口的精确 `address`（并可结合同帧的 `frame["steps"]` 传入可选的 `step` 乐观锁参数，以及从 `frame["variables"]` 读取当前帧业务变量），无需根据 `frame.kind` 或 `frame.cursor` 自行拼接，同时天然支持 `fork_join` 下同一帧内多个并发 `signal` 节点同时挂起的状态观测。

2. **Child Workflow 的信号递归透明路由**：
   当工作流因历史事件数达到阈值而通过 `start_child_workflow` 派生了单层或多层子工作流（`frame.child_execution`）时，挂起节点可能位于某个深层子工作流实例内部。宿主的 `ExecutionService.signal(execution_id, address, output, branch, step)` 按以下确定性规则路由：
   * **固定根工作流 Run**：先通过 `self._describe(root_handle)` 确认根执行存在且为 Axcutor 根工作流，并固定其 `root_run_id = desc.run_id`（若根执行已处于终态，直接抛出 `SignalNotWaiting (409)`）；
   * **沿原始 Query 树逐层向下解析**：
     * 从固定了 `first_execution_run_id=root_run_id` 的根 Handle 开始，查询原始 `get_runtime_snapshot`，记当前层的外部逻辑根前缀为 `current_prefix = snapshot["frames"][0]["address"]`，对应的局部根前缀为 `local_root = current_prefix`；
     * 将外部传入的目标 `address` 转换为当前工作流实例内的局部目标地址 `local_target = local_root + address.removeprefix(current_prefix)`；
     * 检查当前快照中是否存在带有 `child_execution` 的远端帧 `remote_frame`，满足**严格路径段边界匹配**：
       `local_target == remote_frame["address"] or local_target.startswith(remote_frame["address"] + "/")`
       *(注：严禁使用不带 `/` 边界的简单前缀匹配，防止 `main/call` 误匹配 `main/call_other` 或 `main/call[0]`；其中 `==` 对应被调用的子工作流自身即为顶层 `RootRunNode(signal)` 的情形)*；
     * 若匹配到 `remote_frame`，则根据 `remote_frame["child_execution"]`（固定 `workflow_id` 与 `first_execution_run_id=run_id`）获取子工作流 Handle，更新 `current_prefix = current_prefix + remote_frame["address"].removeprefix(local_root)`，继续查询该子工作流的 `get_runtime_snapshot` 以获取其 `local_root = child_snapshot["frames"][0]["address"]` 并递归向下，直至目标地址不再落入任何 `child_execution` 子树；
   * **向目标 Handle 发送 Update 并刷新根视图**：
     * 向最终定位到的 Workflow Handle 发送 `execute_update(AxcutorInterpreterWorkflow.deliver_signal, dumps({"address": local_target, "branch": branch, "output": output, "step": step}))`；
     * Update 完成后，重新查询固定了 `root_run_id` 的根工作流以返回完整最新的 `Execution` 资源快照，对外部调用方完全屏蔽内部 Child Workflow 拆分细节。

---

### 决策 4：宿主 `ExecutionService.signal`、HTTP API 与 CLI 规范

1. **`ExecutionService` 新增方法与异常映射（`python/axcutor_host/src/axcutor_host/service.py`）**：
   ```python
   async def signal(
       self,
       execution_id: str,
       address: str,
       output: JsonObject | None = None,
       branch: str | None = None,
       step: int | None = None,
   ) -> Execution:
   ```
   * **参数预检**：
     * `address` 必须为非空字符串（`isinstance(address, str) and bool(address)`）；否则抛出 `InvalidRequest("address must be a nonempty string")`（HTTP `400`）；
     * `output` 若为 `None`（未传）则默认为空字典 `{}`，若提供则必须为 `dict`；否则抛出 `InvalidRequest("output must be an object")`（HTTP `400`）；
     * `branch` 若提供则必须为非空字符串或 `None`；否则抛出 `InvalidRequest("branch must be a nonempty string or null")`（HTTP `400`）；
     * `step` 若提供则必须为正整数（`type(step) is int and step >= 1`）或 `None`；否则抛出 `InvalidRequest("step must be a positive integer or null")`（HTTP `400`）。
   * **Temporal Update 异常解包与竞态映射**：
     * 若根 `execution_id` 不存在（或带有 `parent_execution` 非根执行），抛出 `ExecutionNotFound`（HTTP `404`）；
     * 调用 `handle.execute_update(...)` 时，若捕获 `temporalio.client.WorkflowUpdateFailedError`：
       * 检查 `error.cause`：若为 `ApplicationError` 且 `type == "SignalNotWaitingError"`（含地址未处于挂起状态或 `step` 不匹配），抛出 **`SignalNotWaiting`（`code = "signal_not_waiting"`，HTTP `409 Conflict`）**；
       * 若 `error.cause` 为 `ApplicationError` 且 `type == "SignalValidationError"`，抛出 **`SignalValidationError`（`code = "signal_validation_failed"`，HTTP `422 Unprocessable Entity`）**；
     * 若在 `describe`/`query` 与 `execute_update` 之间，目标子工作流或根工作流恰好结束（关闭竞态），导致 `execute_update` 或中间子工作流 `query` 抛出 `RPCError(status=NOT_FOUND | FAILED_PRECONDITION)`：由于此时根执行已在入口处确认为存在，严禁将其误映射为根执行的 `404 ExecutionNotFound`，而应统一映射为 **`SignalNotWaiting`（HTTP `409 Conflict`）**；
     * 若捕获 `WorkflowUpdateRPCTimeoutOrCancelledError`（且当前调用方协程本身未被取消）或 Temporal 连接类 `RPCError`，映射为 **`RuntimeUnavailable`（`code = "runtime_unavailable"`，HTTP `503 Service Unavailable`）**（注：`503` 表示投递结果未知，调用方可通过 `GET /v1/executions/{execution_id}` 观察 `Frame.waiting` / `Frame.steps` 确认信号是否已生效）；
     * `SignalNotWaiting` 与 `SignalValidationError` 同步在 `axcutor_host/__init__.py` 的 `__all__` 中公开导出。

2. **HTTP API 新增端点（`python/axcutor_host/src/axcutor_host/http.py`）**：
   ```http
   POST /v1/executions/{execution_id}:signal
   Content-Type: application/json
   ```
   *(注：Starlette 路由注册使用自定义转换器 `/v1/executions/{execution_id:execution_id}:signal`，Playground 后端同步挂载 `/v1/executions/{execution_id:playground_execution_id}:signal`。)*
   * **请求体 Schema（`SignalExecutionRequest`，`additionalProperties: False`）**：
     | 字段 | 类型 | 必填 | 说明 |
     | :--- | :--- | :--- | :--- |
     | `address` | `string`（`minLength: 1`） | 是 | 当前处于 `signal` 挂起等待的目标节点运行时拓扑地址（对应 `Frame.waiting` 中的元素，如 `"__main__/await_approval"`、`"demo.main/batch[0]"` 或顶层 `"__main__"`）。 |
     | `branch` | `string \| null`（`minLength: 1`） | 否 | 当目标节点声明了 `branches` 时必填（选择恢复后的出口分支）；无分支节点省略或传 `null`。 |
     | `output` | `object` | 否 | 缺省时默认 `{}`（若显式传入 `null` 或非对象类型则返回 `400 invalid_request`）；符合目标节点（或所选 `branch`）输出契约的无损 JSON 对象。 |
     | `step` | `integer \| null`（`minimum: 1`） | 否 | 可选的帧步数乐观锁（对应挂起帧的 `Frame.steps`）。若提供，当且仅当目标挂起帧当前 `steps == step` 时才接受信号，否则返回 `409 signal_not_waiting`（用于循环图中防止跨轮次迟到/重发信号误唤醒）。 |
     *(若请求体包含 `address`、`branch`、`output`、`step` 之外的任何未知字段，直接返回 `400 invalid_request`。)*
   * **响应状态码**：
     * `200 OK`：信号已被接受并唤醒节点，且整个工作流在本次唤醒后已同步到达终态（`completed` / `failed` / `cancelled`），响应体为终态 `Execution`；
     * `202 Accepted`：信号已被接受并唤醒节点，工作流仍在继续运行或停留在后续挂起点（`status == "running"`），响应体为最新运行态 `Execution`；
     * `400 Bad Request`（`invalid_json` / `invalid_request`）：JSON 语法错误、包含未知顶层字段，或 `address` / `branch` / `output` / `step` 字段类型非法；
     * `404 Not Found`（`execution_not_found`）：根执行不存在；
     * `409 Conflict`（`signal_not_waiting`）：执行已结束、指定的 `address` 当前未处于 `signal` 挂起状态，或传入的 `step` 与当前挂起帧步数不匹配；
     * `422 Unprocessable Entity`（`signal_validation_failed`）：`branch` 或 `output` 违反了该 `signal` 节点的静态类型契约（工作流保持原挂起状态不变，零 Event History 写入）；
     * `503 Service Unavailable`（`runtime_unavailable`）：Temporal 后端不可达或 Update RPC 等待超时（投递结果未知，可通过查询 `Execution` 确认）。

3. **CLI 新增子命令（`python/axcutor_host/src/axcutor_host/cli.py`）**：
   ```sh
   axcutor signal <execution_id> <address> [--branch <branch_id>] [--output <json_file>] [--step <step>] [--wait]
   ```
   * **避免与全局 `--address` 参数冲突**：由于 `_add_connection_arguments` 已使用 `args.address` 存储 Temporal Server 连接地址，`signal` 子命令的位置参数在 `argparse` 中必须使用 `parser.add_argument("target_address", metavar="address", ...)` 存储至 `args.target_address`；
   * **输入解析与 `--wait` 行为**：
     * 若指定 `--output <json_file>`，使用无损 `codec.loads` 读取文件并校验顶层必为 `dict`（否则报 `InvalidRequest("output must be an object")`）；省略时默认为 `{}`；
     * 调用 `execution = await service.signal(args.execution_id, args.target_address, outputs, args.branch, args.step)`；
     * 若指定 `--wait` 且 `execution["status"] == "running"`，继续调用 `execution = await service.wait(args.execution_id)` 等待工作流到达终态；
     * 最终输出一行 `codec.dumps(execution)` JSON，且当指定了 `--wait` 但最终 `execution["status"] != "completed"` 时，与 `axcutor run` 保持一致以 `raise SystemExit(1)` 非零状态码退出。

---

## 三、 完整使用示例

### 1. 包含前置通知、`signal` 挂起等待与后置处理的完整工作流（`__axcutor__.yaml`）

```yaml
input:
  order_id: { type: string }
  amount: { type: number }
output:
  final_status: { type: string }
  reviewer: { type: string }

graph:
  variables:
    ticket_id: { type: string }
    approved_by: { type: string }
    reject_reason: { type: string }

  nodes:
    # 第一步：通过普通 Python 节点发送外部审批通知
    notify_reviewer:
      input: [order_id, amount]
      output:
        ticket_id: { type: string }
      run:
        executor: builtin
        content: |
          return Output(ticket_id=f"TICKET-{input.order_id}")

    # 第二步：进入纯等待 signal 执行器，Temporal Workflow 在此挂起，零 Worker 占用
    await_decision:
      input: [order_id, ticket_id]
      output:
        approved_by: { type: string }
        reject_reason: { type: string }
      branches:
        approved:
          description: "审批通过"
          output: [approved_by]
        rejected:
          description: "审批驳回"
          output: [reject_reason]
      timeout_ms: 86400000
      catch:
        timed_out: [timeout]
      run:
        executor: signal

    # 第三步 A：审批通过后的后置处理节点
    fulfill_order:
      input: [order_id, approved_by]
      output:
        final_status: { type: string }
        reviewer: { type: string }
      run:
        executor: builtin
        content: |
          return Output(final_status="APPROVED", reviewer=input.approved_by)

    # 第三步 B：审批驳回或超时后的回退节点
    cancel_order:
      input: [order_id]
      output:
        final_status: { type: string }
        reviewer: { type: string }
      run:
        executor: builtin
        content: |
          return Output(final_status="REJECTED", reviewer="system")

  edges:
    - start -> notify_reviewer
    - notify_reviewer -> await_decision
    - await_decision.approved -> fulfill_order
    - await_decision.rejected -> cancel_order
    - await_decision.timed_out -> cancel_order
    - fulfill_order -> end
    - cancel_order -> end
```

### 2. 运行期查询与外部信号恢复交互流程

1. **查询挂起中的执行状态（`GET /v1/executions/order-101`）**：
   工作流执行完 `notify_reviewer` 后停在 `await_decision`，`Frame` 快照显示 `cursor: "await_decision"`、`steps: 2` 且 `waiting: ["__main__/await_decision"]`：
   ```json
   {
     "execution_id": "order-101",
     "status": "running",
     "started_at": "2026-11-09T02:00:00Z",
     "finished_at": null,
     "frames": [
       {
         "kind": "function",
         "address": "__main__",
         "cursor": "await_decision",
         "waiting": ["__main__/await_decision"],
         "call_depth": 1,
         "steps": 2,
         "max_steps": 1000,
         "variables": {
           "order_id": "ORD-99",
           "amount": 1250.5,
           "ticket_id": "TICKET-ORD-99"
         },
         "children": []
       }
     ],
     "result": null,
     "error": null
   }
   ```

2. **外部提交非法信号（Validator 契约校验失败，返回 `422`，工作流保持挂起且零历史事件写入）**：
   ```http
   POST /v1/executions/order-101:signal
   Content-Type: application/json

   {
     "address": "__main__/await_decision",
     "branch": "approved",
     "output": {}
   }
   ```
   因 `approved` 分支要求必填字段 `approved_by`，响应返回 `422 Unprocessable Entity`（`code: "signal_validation_failed"`），`order-101` 继续停留在 `await_decision` 等待合法信号。

3. **外部提交合法信号（可附带 `step: 2` 乐观锁，恢复执行并完成工作流）**：
   ```http
   POST /v1/executions/order-101:signal
   Content-Type: application/json

   {
     "address": "__main__/await_decision",
     "branch": "approved",
     "step": 2,
     "output": {
       "approved_by": "alice"
     }
   }
   ```
   Update 校验通过并唤醒 `await_decision`，工作流自动推进至 `fulfill_order` 并完成。

---

## 四、 变更影响模块清单

| 模块路径 | 变更内容摘要 |
| :--- | :--- |
| `crates/axcutor-core/src/ast/mod.rs` | 在 `RunSpec` 枚举中新增 `Signal` 变体，手写 `Serialize` 序列化为 `{"executor": "signal"}`。 |
| `crates/axcutor-core/src/compiler/schema.rs` | 在 `run()` 解析器中优先匹配 `executor == "signal"` 并强制闭合校验 `closed(m, &["executor"], a, ds)`；对 `signal` 节点将 `retry.on` 与 `catch` 的允许异常选择器收紧为 `{"timeout"}`（默认 `retry.on` 归一为 `["timeout"]`，通配符 `"*"` 针对 `{"timeout"}` 求补）。 |
| `python/axcutor_runtime/src/axcutor_runtime/workflow.py` | 在 `_state_machine` 中区分 `is_activity` 与 `signal`（确保 `signal` 走 Workflow 重试循环与 `_with_timeout`）；新增 `_SignalWaiter` 表、`_wait_signal` 挂起方法、`@deliver_signal.validator` 只读校验器（含地址核验、可选 `step` CAS 核验与契约校验）与 `@workflow.update` `deliver_signal` 处理器（采用 `codec` 无损 JSON 字符串传输）；在 `get_runtime_snapshot` 的 `Frame` 中导出 `waiting: list[str]`。 |
| `python/axcutor_host/src/axcutor_host/__init__.py` | 导出新增的 `SignalNotWaiting` 与 `SignalValidationError` 宿主异常类。 |
| `python/axcutor_host/src/axcutor_host/service.py` | 新增 `SignalNotWaiting`（409）与 `SignalValidationError`（422）异常类；在 `Frame` TypedDict 中包含 `waiting: list[str]` 并同步子工作流地址前缀替换；在 `ExecutionService` 中实现支持 `step` 乐观锁、多层 Child Workflow 递归路由穿透与异常解包的 `signal()` 方法。 |
| `python/axcutor_host/src/axcutor_host/http.py` | 在 `_STATUS`、OpenAPI `Frame` / `errors` 中补充 `waiting` 与 `409`/`422` 错误映射；新增 `POST /v1/executions/{execution_id:execution_id}:signal` 路由与闭合的 `SignalExecutionRequest` Schema（含可选 `step`）。 |
| `python/axcutor_host/src/axcutor_host/cli.py` | 新增 `axcutor signal` 子命令（位置参数使用 `dest="target_address"` 避免与全局 `--address` 冲突，支持 `--branch`、`--output`、`--step` 与 `--wait`）。 |
| `playground/` | 在 `playground/backend/src/axcutor_playground/http.py` 同步添加错误码与 `:signal` 路由，并更新 `playground/openapi.json` 与前端生成的 TypeScript 客户端类型。 |
| `manual/spec/` 与 `manual/docs/` | 同步更新 `RunSpec` 文法（排除扩展分支中的 `"signal"`）、`signal` 节点 `retry`/`catch` 异常选择器规则、执行器 ABI 规范与 HTTP/CLI 参考文档。 |
