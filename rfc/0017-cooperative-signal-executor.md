# RFC-0017: 协作式挂起执行器 `signal`、Temporal Update 恢复协议与宿主 `:signal` 接口规范

* **状态**：Proposed
* **所属分类**：执行器 ABI 扩展、Temporal 确定性挂起调度与宿主 HTTP/CLI 恢复协议（Cooperative Signal Executor, Workflow Update Suspension & Host Signal API）
* **前置关联提案**：
  * [`RFC-0011: Axcutor 编译器前后端与 Temporal 运行时参考实现架构`](./0011-compiler-and-temporal-runtime-architecture.md)
  * [`RFC-0012: 结构化 run 执行器分派机制、移除自然语言变量正则提取与 builtin 可信 Python 执行器规范`](./0012-run-executor-and-builtin-python.md)
  * [`RFC-0014: 基于预置 Python 模块引用的 module 执行器、__axcutor__.yaml 包同构布局与契约模板 SHA 校验规范`](./0014-module-python-executor.md)
* **核心解决的问题**：
  1. **新增纯等待内置执行器 `signal`（`run: { executor: "signal" }`）**：在不引入节点内 Python 栈序列化、重放缓存（Replay Journal）或长周期 Activity 进程占用的前提下，将“挂起等待外部消息输入”建模为独立的原子 `run` 执行器，挂起前后的外部通知或后置处理交由控制流图（CFG）中前后相邻的 `builtin` / `module` 节点完成；
  2. **基于 Temporal Workflow `wait_condition` 与 `@workflow.update` 实现零 Activity 开销的确定性挂起与唤醒**：`signal` 节点在 `AxcutorInterpreterWorkflow` 协程内直接挂起，不发起 Temporal Activity、不占用 Worker 槽位、不产生心跳流量；通过 `@workflow.update` 接收外部信号并同步执行 `axcutor_core.validate_run_contract` 校验；
  3. **确立“契约校验失败视为非法信号（`422`）而非节点 `contract` 异常”的边界隔离准则**：外部提交的信号若不符合目标挂起节点的 `(output, branches)` 契约，直接在 Update / HTTP 边界拒绝并返回 `422 signal_validation_failed`，挂起节点继续保持等待状态，避免因外部误传载荷导致长周期工作流意外进入失败或异常分支；
  4. **扩展宿主服务、HTTP API（`POST /v1/executions/{execution_id}:signal`）与 CLI（`axcutor signal`）**：支持按节点运行时拓扑地址 `address`（自动穿透 `child_execution` 派生的 Temporal Child Workflow）精确投递信号，并在 `Frame` 视图中暴露实时挂起等待状态。

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
* 因此，信号契约校验必须在接收边界（Temporal `@workflow.update` 处理器）同步完成：**校验不通过时直接向调用方报错拒绝，工作流挂起状态保持分毫无损**。

---

## 二、 核心设计决策

### 决策 1：新增闭合内置执行器 `RunSpec::Signal`（`run: { executor: "signal" }`）

1. **`RunSpec` 文法扩展**：
   在 [`01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md) 的 `RunSpec` 标记联合体中新增内置分支 `SignalRunSpec`：
   ```txt
   RunSpec        ::= BuiltinRunSpec
                    | ModuleRunSpec
                    | SignalRunSpec
                    | ExtensibleExecutorRunSpec ;

   SignalRunSpec  ::= {
     executor: "signal"
   } ;
   ```
   * **闭合性约束**：当 `executor == "signal"` 时，`run` 映射对象必须且只能包含 `executor: "signal"` 一个键（无需也不允许声明 `content` 或任何其他字段）；若包含任何额外字段，编译期 Pass 1 在 `/run/<field>` 处报 `schema.unknown_field` 错误。
   * **AST 表示（`crates/axcutor-core/src/ast/mod.rs`）**：
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
2. **与现有节点修饰符的正交配合**：
   * **`input`、`output`、`branches`**：
     * `input` 声明该 `signal` 节点在挂起时捕获的只读上下文变量（外部通过查询接口观察挂起帧时，可从帧及关联上下文获知当前挂起节点所处的业务上下文）；
     * `output` 与 `branches` 直接定义外部信号恢复该节点时必须满足的静态契约（無需额外声明独立的 message schema）。
   * **`timeout_ms`**：
     * 若 `signal` 节点声明了 `timeout_ms`，表示该节点等待外部信号的最大允许时长（使用 Workflow 虚拟时钟 ` _with_timeout` 计时，无默认 7 天 Activity 超时限制；若未声明 `timeout_ms`，则无限期挂起直至收到信号或工作流被取消）。
     * 一旦在收到合法信号前触及 `timeout_ms`，立即取消挂起等待并产生标准可恢复异常 **`timeout`**，进入节点的 `retry` / `catch` 状态机。
   * **`retry` 与 `catch`**：
     * 编译期修饰符校验保持原子 `run` 节点的统一规则不变；
     * 运行期由于 `signal` 节点不执行外部代码且非法信号在边界直接拒绝，运行期仅可能因 `timeout_ms` 产生 `timeout` 可恢复异常（被 `retry.on` 包含 `timeout` 时重置计时重新等待，或被 `catch` 捕获后转入异常分支）。
   * **`for_each` 与 `fork_join`**：
     * 支持在 `for_each` 或 `fork_join` 并发分支中使用 `signal` 节点；每个并发实例拥有唯一确定的运行时拓扑地址 `address`（如 `main/approve_items[0]`、`main/approve_items[1]`），外部可按 `address` 逐个独立投递信号。
   * **契约生成（`axcutor contracts`）**：
     * `generate_contracts` 仅针对 `executor == "module"` 生成 `.py` 契约模板，遇到 `executor == "signal"` 时自动跳过，不产生任何宿主文件。

---

### 决策 2：基于 Temporal Workflow `wait_condition` 与 `@workflow.update` 的纯等待执行机制

1. **零 Activity 调度的挂起生命周期**：
   在 `AxcutorInterpreterWorkflow._attempt`（[`workflow.py`](../python/axcutor_runtime/src/axcutor_runtime/workflow.py)）中，当 `body["kind"] == "run"` 且 `body["run"]["executor"] == "signal"` 时，解释器**不调用 `_run_activity`**，而是直接进入 Workflow 内存挂起流程 `_wait_signal(frame, node, snapshot, address)`：
   * 为当前激活定位所属的目标栈帧：
     * 对于顶层 `RootRunNode`（`address == entry`，`frame.cursor == "run"`）或 `for_each` 的 `IterationFrame`（`frame.address == address`），挂起目标地址即为 `address`；
     * 对于普通图内 `ActionNode`（父图帧 `frame.address` 为所属图地址，`frame.cursor == node_id`），该节点的拓扑地址为 `address = f"{frame.address}/{node_id}"`；
   * 在 Workflow 实例的挂起注册表 `self._waiters: dict[str, _SignalWaiter]` 中，以 `address` 为键登记一个等待槽位 `_SignalWaiter`：
     ```python
     @dataclass
     class _SignalWaiter:
         frame_id: int
         address: str
         input_snapshot: JsonObject
         output_schema: JsonObject
         branches_contract: JsonObject
         response: JsonObject | None = None
     ```
   * 调用 `await workflow.wait_condition(lambda: waiter.response is not None)` 挂起当前协程（若节点声明了 `timeout_ms`，外层由 `_with_timeout` 统一包裹，并在 `finally` 块中无条件从 `self._waiters` 移除 `address`，确保超时、并发 Fail-Fast 取消或正常唤醒后绝不残留僵尸等待槽位）。
2. **`@workflow.update` 同步校验与唤醒协议**：
   在 `AxcutorInterpreterWorkflow` 上声明 `@workflow.update` 方法 `deliver_signal(request: JsonObject) -> JsonObject`：
   * 请求结构为 `{"address": str, "branch": str | None, "output": JsonObject}`；
   * **步骤 1（地址与挂起状态核验）**：
     * 检查 `address` 是否存在于当前 `self._waiters` 中；
     * 若不存在（目标节点尚未到达、已完成、已超时或地址拼写错误），抛出 `exceptions.ApplicationError(f"no signal node waiting at {address!r}", type="SignalNotWaitingError", non_retryable=True)`；
   * **步骤 2（契约边界同步校验——拒绝非法信号且不污染工作流状态）**：
     * 将 `{"branch": request.get("branch"), "output": request["output"]}` 交由 `axcutor_core.validate_run_contract(raw_response, dumps(waiter.output_schema), dumps(waiter.branches_contract))` 执行严格校验（含分支合法性、必填输出字段完备性、深层 `TypeSpec` Schema 校验与顶层多余字段白名单过滤）；
     * 若抛出 `axcutor_core.ContractError`，立即在 Update 处理器内抛出 `exceptions.ApplicationError(str(error), type="SignalValidationError", non_retryable=True)`；
     * **关键不变量**：此时 `waiter.response` 仍保持 `None`，`wait_condition` **不会被唤醒**，节点也**不会**触发 `contract` 异常或消耗 `retry` 次数；Temporal Update 将该错误原样返回给宿主，由宿主映射为 HTTP `422 Unprocessable Entity`；
   * **步骤 3（原子写入与协程唤醒）**：
     * 校验通过后，将规范化后的结果赋给 `waiter.response = loads(validated_json)` 并从 `self._waiters` 中弹出该 `waiter`；
     * `workflow.wait_condition` 立即在当前 Workflow Task 中唤醒，`_wait_signal` 返回 `waiter.response`，解释器将输出原子更新至 `Frame.env` 并推进 `frame.cursor` 至下一跳节点。

---

### 决策 3：运行时快照（`Frame`）可观测性与 Child Workflow 透明穿透

1. **`Frame` 挂起状态暴露**：
   为了使外部调用方与 Web Playground 无需猜测当前有哪些节点正在等待信号，在 `Frame` 诊断快照中暴露当前帧关联的挂起等待地址：
   * 当某个节点处于 `signal` 挂起状态时，在 `get_runtime_snapshot`（及宿主对外返回的 `Frame` 结构）中，可通过帧的 `cursor` 与新增的布尔标志或挂起列表直接识别；
   * 为保持现有 `Frame` Schema 的最小侵入与完全向后兼容，在 `Frame` 上新增字段 `waiting: bool`（普通执行中为 `false`，当且仅当该帧当前正停留在 `executor: "signal"` 挂起等待时为 `true`）：
     * 对顶层 `RootRunNode`（`address = entry, cursor = "run"`）：收到信号的目标 `address` 为 `frame.address`（即 `entry`）；
     * 对 `IterationFrame`（`address = ".../node[i]", cursor = "node"`）：收到信号的目标 `address` 为 `frame.address`；
     * 对停留在图内 `ActionNode` 的 `FunctionFrame` / `BlockFrame`（`address = "<graph_addr>", cursor = "<node_id>"`）：收到信号的目标 `address` 为 `f"{frame.address}/{frame.cursor}"`（当 `frame.waiting == True` 时）。
     *(注：同时支持在 `deliver_signal` 中接受顶层 `RootRunNode` 的 `entry` 或 `f"{entry}/run"`，但在规范中统一规定：节点级挂起地址与子图/迭代寻址同构，图内节点为 `<frame.address>/<node_id>`，迭代元素为 `<frame.address>`，顶层 `RootRunNode` 为 `<entry>`。)*
2. **Child Workflow 的信号透明路由**：
   当工作流因历史事件数达到阈值而通过 `start_child_workflow` 派生了子工作流（`frame.child_execution`）时，挂起节点可能位于某个子工作流实例内部：
   * 如同 `ExecutionService._frames` 在查询时递归拼接 `child_execution` 的地址前缀一样，宿主的 `ExecutionService.signal(execution_id, address, output, branch)` 在投递信号时：
     * 先查询根工作流的 `get_runtime_snapshot`；
     * 若目标 `address` 落在某个带有 `child_execution` 的远端帧子树下，宿主自动将外部逻辑 `address` 还原为该子工作流内部的局部地址，并直接向对应的 `child_execution` Workflow Handle 发送 `deliver_signal` Update；
     * 完成后返回根工作流的最新 `Execution` 视图，对外部调用方完全屏蔽内部 Child Workflow 拆分细节。

---

### 决策 4：宿主 `ExecutionService.signal`、HTTP API 与 CLI 规范

1. **`ExecutionService` 新增方法（`python/axcutor_host/src/axcutor_host/service.py`）**：
   ```python
   async def signal(
       self,
       execution_id: str,
       address: str,
       output: JsonObject | None = None,
       branch: str | None = None,
   ) -> Execution:
   ```
   * **参数预检**：
     * `address` 必须为非空字符串；否则抛出 `InvalidRequest("address must be a nonempty string")`（HTTP `400`）；
     * `output` 若省略则默认为空字典 `{}`，若提供则必须为 `dict`；否则抛出 `InvalidRequest("output must be an object")`（HTTP `400`）；
     * `branch` 若提供则必须为非空字符串或 `None`；否则抛出 `InvalidRequest("branch must be a nonempty string or null")`（HTTP `400`）。
   * **执行状态与异常映射**：
     * 若目标 `execution_id` 不存在，抛出 `ExecutionNotFound`（HTTP `404`）；
     * 若目标执行已处于终态（`completed`、`failed`、`cancelled`），或目标 `address` 当前并未处于 `signal` 挂起等待状态（捕获 `SignalNotWaitingError`），抛出新增的宿主错误 **`SignalNotWaiting`（`code = "signal_not_waiting"`，HTTP `409 Conflict`）**；
     * 若信号载荷未通过目标节点的 `(output, branches)` 契约校验（捕获 `SignalValidationError`），抛出新增的宿主错误 **`SignalValidationError`（`code = "signal_validation_failed"`，HTTP `422 Unprocessable Entity`）**；
     * 若投递成功，Update 完成后立即读取并返回最新的 `Execution` 资源快照（与 `submit`、`get`、`cancel` 保持统一的返回模型）。

2. **HTTP API 新增端点（`python/axcutor_host/src/axcutor_host/http.py`）**：
   ```http
   POST /v1/executions/{execution_id}:signal
   Content-Type: application/json
   ```
   * **请求体 Schema（`SignalExecutionRequest`）**：
     | 字段 | 类型 | 必填 | 说明 |
     | :--- | :--- | :--- | :--- |
     | `address` | `string` | 是 | 当前处于 `signal` 挂起等待的目标节点运行时拓扑地址（如 `"__main__/await_approval"`、`"demo.main/batch[0]"` 或顶层 `"__main__"`）。 |
     | `branch` | `string \| null` | 否 | 当目标节点声明了 `branches` 时必填（选择恢复后的出口分支）；无分支节点省略或传 `null`。 |
     | `output` | `object` | 否 | 默认 `{}`；符合目标节点（或所选 `branch`）输出契约的无损 JSON 对象。 |
   * **响应状态码**：
     * `200 OK`：信号已被接受并唤醒节点，且整个工作流在本次唤醒后已同步到达终态（`completed` / `failed` / `cancelled`），响应体为终态 `Execution`；
     * `202 Accepted`：信号已被接受并唤醒节点，工作流仍在继续运行或停留在后续挂起点（`status == "running"`），响应体为最新运行态 `Execution`；
     * `400 Bad Request`（`invalid_json` / `invalid_request`）：JSON 语法错误或请求体结构非法；
     * `404 Not Found`（`execution_not_found`）：执行不存在；
     * `409 Conflict`（`signal_not_waiting`）：执行已结束，或指定的 `address` 当前未处于 `signal` 挂起状态；
     * `422 Unprocessable Entity`（`signal_validation_failed`）：`branch` 或 `output` 违反了该 `signal` 节点的静态类型契约（工作流保持原挂起状态不变）；
     * `503 Service Unavailable`（`runtime_unavailable`）：Temporal 后端不可达或 Worker 未响应 Update。

3. **CLI 新增子命令（`python/axcutor_host/src/axcutor_host/cli.py`）**：
   ```sh
   axcutor signal <execution_id> <address> [--branch <branch_id>] [--output <json_file>] [--wait]
   ```
   * 读取 `--output` 指定的无损 JSON 文件（省略时为 `{}`）及可选的 `--branch`，调用 `service.signal(execution_id, address, outputs, branch)` 并输出最新 `Execution` JSON；若指定 `--wait`，则继续调用 `service.wait(execution_id)` 等待工作流到达终态并输出终态 `Execution` JSON。

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
   工作流执行完 `notify_reviewer` 后停在 `await_decision`，`Frame` 快照显示 `cursor: "await_decision"` 且 `waiting: true`：
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
         "waiting": true,
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

2. **外部提交非法信号（契约校验失败，返回 `422`，工作流保持挂起）**：
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

3. **外部提交合法信号（恢复执行并完成工作流）**：
   ```http
   POST /v1/executions/order-101:signal
   Content-Type: application/json

   {
     "address": "__main__/await_decision",
     "branch": "approved",
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
| `crates/axcutor-core/src/ast/mod.rs` | 在 `RunSpec` 枚举中新增 `Signal` 变体，序列化为 `{"executor": "signal"}`。 |
| `crates/axcutor-core/src/compiler/schema.rs` | 在 `run()` 解析器中支持 `executor == "signal"`，强制闭合校验 `closed(m, &["executor"], a, ds)`。 |
| `python/axcutor_runtime/src/axcutor_runtime/workflow.py` | 在 `Frame` 中维护 `waiting: bool` 状态；新增 `_SignalWaiter` 表、`_wait_signal` 挂起方法与 `@workflow.update` `deliver_signal` 处理器，在 Update 内调用 `axcutor_core.validate_run_contract` 同步校验信号契约。 |
| `python/axcutor_host/src/axcutor_host/service.py` | 新增 `SignalNotWaiting`（409）与 `SignalValidationError`（422）异常类；在 `Frame` TypedDict 中包含 `waiting: bool`；在 `ExecutionService` 中实现支持 Child Workflow 路由穿透的 `signal()` 方法。 |
| `python/axcutor_host/src/axcutor_host/http.py` | 新增 `POST /v1/executions/{execution_id}:signal` 路由与 OpenAPI 3.1 `SignalExecutionRequest` / `signalExecution` Schema 定义。 |
| `python/axcutor_host/src/axcutor_host/cli.py` | 新增 `axcutor signal` 子命令。 |
| `manual/spec/` 与 `manual/docs/` | 同步更新 `RunSpec` 文法、执行器 ABI 规范与 HTTP API 参考文档。 |
