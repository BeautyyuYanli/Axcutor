# 解释器与执行器接口（0.1）

运行语义依据 `manual/spec/04-runtime-execution-model` 与 `05-executor-abi`。解释器直接读取[编译器契约](compiler-api.md)的 `{entry, workflows}` bundle 和 `routes`，不重新编译、加载源文件或访问业务资源。只应提交成功编译且未经修改的 bundle。工作流定义及宿主选项都进入 Temporal History，重放不重新读取进程配置。

## 编译与启动

Python 包最低支持 3.11；原生 `axcutor_core` 包仍支持 3.10。从仓库根目录运行 `uv sync --locked --python 3.12` 会构建本地 PyO3 扩展并安装 `axcutor_runtime`。

```python
from temporalio.client import Client
from axcutor_runtime import AxcutorInterpreterWorkflow, compile_bundle
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.worker import create_worker

async def echo(request, abort_signal):
    abort_signal.raise_if_aborted()
    return {"output": {"answer": request["input_snapshot"]["n"]}}

async def execute():
    client = await Client.connect("localhost:7233")
    bundle_json = compile_bundle("main", {
        "main": {"format": "yaml", "text": """
input:
  n: {type: integer}
output:
  answer: {type: integer}
run: echo
"""}
    })
    async with create_worker(client, "axcutor", {"echo": echo}):
        result_json = await client.execute_workflow(
            AxcutorInterpreterWorkflow.run,
            {"bundle_json": bundle_json, "input_json": dumps({"n": 42}), "options": {}},
            id="example-42", task_queue="axcutor",
        )
        return loads(result_json)  # {"branch": None, "output": {"answer": 42}}
```

`compile_bundle(entry, sources)` 接收既有 `{format: "json" | "yaml", text}` 源快照映射并返回 bundle JSON 文本。`CompilationError` 的 `diagnostics` 和 `result_json` 保留 Rust 原始结构化诊断；编译失败不启动 Workflow。

`run(request)` 接收 `{bundle_json: str, input_json: str, options?: object}`，返回 `{branch, output}` JSON 文本。入口参数必须完整、类型正确且没有多余参数。入口错误与引擎不变量错误直接失败，不进入 DSL catch。`_entry`、`_call_depth` 为解释器派生 Child Workflow 的保留请求字段。

## 固化的宿主选项

| options 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `activity_timeout_ms` | `604800000`（7 天） | run 未声明 timeout_ms 时的有限单次 Activity 期限 |
| `heartbeat_interval_ms` | `1000` | Activity 包装器的心跳周期 |
| `heartbeat_timeout_ms` | `10000` | Temporal 心跳超时，必须大于心跳周期 |
| `max_call_depth` | `64` | 根深度为 1，workflow 或内联 graph 每进入一层加 1 |
| `task_queues` | `{}` | run_spec 到 Activity task queue 的精确字符串映射；未匹配使用 Workflow 队列 |
| `child_workflow_history_events` | `null` | 可选正整数；到达调用边界且当前历史事件数达到阈值时提前使用 Child Workflow |

期限、心跳和深度必须为正整数；未知选项拒绝。Activity 的有限默认期限是基础设施边界，DSL 未声明超时也可能由它产生 timeout。需要更长期限时由提交方明确设置。Worker 的 SDK 资源配置可通过 `create_worker(..., **kwargs)` 设置；影响执行语义的配置必须放在请求 options 中。

默认在 Temporal 建议继续新历史时，将下一次 workflow 调用交给同一解释器类型的 Child Workflow；也可用显式事件阈值提前拆分。普通调用使用当前 Workflow 内的函数帧。Child 携带相同 bundle、选项、绑定后的输入及递增深度，禁用隐式 Workflow 重试，父取消等待子执行结束。此边界只隔离后续被调用工作流的历史，不清空父历史，也不自动压缩任意长根循环。

## 纯函数文本内核

Rust `axcutor_core::kernel` 提供类型化纯函数及 `_json` 适配器。Python 扩展公开：

```text
eval_arg(arg_json, env_json) -> value_json
eval_builtin(op, bindings_json, env_json, symbols_json) -> result_json
validate_run_contract(response_json, output_schema_json, branches_json) -> result_json
validate_input(input_json, schema_json) -> input_json
```

`ArgValue`、`Bindings`、`TypeSpec` 沿用编译 AST。`op` 使用 `assign`、`array_get` 等现有枚举文本；`symbols_json` 是当前图及迭代注入的类型环境。builtin 返回 `{branch, output}`，输出已按绑定重命名。none/empty 缺席字段不进入 delta，只有宿主解释器应用编译好的路由 kill 集；array_shift.empty 保留旧 rest。

契约校验只投影所选正常分支承诺的输出，忽略其他顶层字段，拒绝缺字段、非法分支和深层类型违约。闭合 TypedDict 的额外属性仍拒绝。`ContractError(ValueError)` 对应可恢复 contract；其他参数/入口错误为 ValueError；内核不变量或非有限算术结果为 RuntimeError。文本适配器拒绝重复键并复用核心有界解码，值树上限为 132 层（含模型外壳），不会为无效输入补默认值。

## 帧、并发与错误

函数和内联帧分别只接收显式输入/捕获、迭代注入和自己的 default。每次尝试从同一份最小读取快照开始。输出全量校验通过后一次提交；失败尝试、私有变量和业务函数原地修改不污染父帧。

Fork-Join 锁定共同快照，全部成功后按声明顺序合并。for_each 支持 run、内联 graph 和 workflow 调用，按并发配额启动元素，数组压缩缺席槽位，OrderedMap 按源键顺序汇聚；空集合初始化目标为空集合且计 0 步。语法不允许直接在 builtin 上挂 for_each。任一未捕获故障会停止后续派发、取消并等待同组任务，再丢弃全部槽位。

每图独立计步；普通节点每激活一次计 1，retry 不额外计步，迭代每取得配额并准备启动一个元素计 1。根超步数失败为 `MaxStepsExceededError`，子图超步数向父节点传递可恢复 `max_steps`。深度超限为 `StackOverflowError`。引擎错误和外部取消穿透所有 DSL retry/catch；根入口将引擎错误转为明确失败的 Workflow，避免 Workflow Task 无限重试。

可恢复选择器为 execution、timeout、contract、max_steps。run 的正固定退避、包含 timeout 且不包含 contract 的重试，在 SDK 可精确表达时下推为 Activity RetryPolicy；max_attempts 包含首次尝试，系数为 1。包含 contract、不包含 timeout 或零退避时使用 Workflow 循环，底层 Activity 每次只尝试 1 次。contract 也可能由 Workflow 对 Activity 成功响应的二次校验产生，因此其重试由同一 Workflow 循环管理整个节点的总尝试预算，避免内外重试叠加。复合节点的单次期限由 Workflow 定时器管理，超时等待子任务清理后进入 retry/catch。

## 异步业务执行器

注册表按完整 run_spec 字符串选择异步 Python 函数。函数签名为 `async (request, abort_signal) -> {branch?, output}`。request 包含 `run_spec`、最小 `input_snapshot`、`output_schema`、`branches_contract`、`attempt_timeout_ms` 和 `heartbeat_interval_ms`；业务函数持有独立副本，修改契约不能放宽包装器自己的校验。缺少注册项和业务异常映射为 execution。

Workflow 与 Activity 之间的业务载荷都是 JSON 文本。Activity 在向 Server 返回之前调用 Rust 校验；Workflow 对成功响应再次校验。Activity 使用 WAIT_CANCELLATION_COMPLETED，包装器持续心跳以接收 Server 取消，并持有本地单次期限。AbortSignal 提供 `aborted`、`reason`、`wait()` 与 `raise_if_aborted()`。取消或本地期限触发后，信号置位、业务任务取消；即使业务函数吞取消后返回或抛出迟到错误，也只等待其退出并丢弃结果。心跳、期限和取消监听任务全部在退出时清理。

此适配器只接收异步函数。执行器须响应取消、释放连接/子进程/事务等资源；不能无限阻塞事件循环或无限吞取消。解释器回滚只覆盖变量，不回滚已发生的外部副作用，幂等键和事务由业务执行器负责。Server 已宣告 Activity 超时后不会等待另一台 Worker 的资源清理，故外部副作用仍需业务幂等与取消协议。

## 数值、Query 与重放

codec 使用保序 dict、Python int 和 Decimal 传输精确 JSON 数字，不使用 Decimal 做解释器算术，也不修改全局精度或整数位数设置。`1e0` 等浮点词法在重编码后仍为浮点数字。超出 Decimal 指数或 Python 整数字符串位数边界的数字由 simplejson.RawJSON 保留原始数字文本；业务函数遇到这类极值可继续透明传输，算术仍应交给 Rust 内核。数值不转换成 JSON 字符串。JSON 数字拼写可能规范化（如指数补 `+`），数值、整数/浮点分类和对象键序保持不变。

Rust 对整数加减乘和数学取模保持精确；number 运算和真除法使用 f64，非有限结果使 Workflow 以 EngineError 失败。比较与深度相等在转换 f64 之前保持精确实数语义，布尔不作为数字。

`await handle.query(AxcutorInterpreterWorkflow.get_runtime_snapshot)` 返回 `{frames: [...]}`，每个节点含 id、parent_id、kind、address、call_depth、cursor、steps、max_steps、env_json 和 children。Query 只读取当前 Workflow 的活跃帧树，不执行节点或查询 Child；需要时可单独查询 Child Workflow。帧退出后从树中移除。环境使用 JSON 文本，保留数值与键序；调用方修改返回值不影响执行。

`create_worker` 配置真实 SandboxedWorkflowRunner，仅透传确定性的 `axcutor_core` 与 `simplejson`；业务函数注册表由 Activity Worker 持有，位于 Workflow 沙箱之外。离线 Replayer 可复用 `axcutor_runtime.worker.workflow_runner()`。有关透传与测试服务参见 [Temporal sandbox 文档](https://docs.temporal.io/develop/python/best-practices/python-sdk-sandbox)和 [WorkflowEnvironment API](https://python.temporal.io/temporalio.testing.WorkflowEnvironment.html)。测试使用真实扩展、sandbox、time-skipping Server，并重放父及派生 Child 的实际 History；本地 watchdog 单独使用真实 SDK ActivityEnvironment 验证。
