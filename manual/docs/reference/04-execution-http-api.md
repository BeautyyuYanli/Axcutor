# 04. Execution HTTP API

`axcutor-playground api` 启动独立执行 HTTP API，提交、查询、取消与信号投递接口统一返回 `Execution` 资源。源码闭包与初始输入在提交端同步编译并完成契约预检，通过后才创建持久化执行。API 使用惰性 Temporal Client，不创建 Worker、工作区或执行缓存；重启 API 不会取消已提交的执行。

Worker 由独立的 `axcutor worker` 进程（由 `axcutor-cli` 包提供）运行，提交和轮询必须使用相同的 namespace 与 task queue。例如，在两个终端分别启动：

```sh
uv run --locked axcutor worker --task-queue axcutor
uv run --locked axcutor-playground api --task-queue axcutor --port 8000
```

两者均支持 `--address`、`--namespace`（默认读取 `TEMPORAL_ADDRESS`、`TEMPORAL_NAMESPACE`），任务队列默认读取 `AXCUTOR_TASK_QUEUE`。`axcutor-playground api` 支持 `--host` 和 `--port`，默认监听 `127.0.0.1:8000`。

不带子命令的 `axcutor-playground` 启动完整工作台，默认监听 `127.0.0.1:8080`。工作台管理本地工作区与专用队列上的驻留 Worker，通过 `/v1/workspace/executions` 提交执行，在运行与本地清理期间校验工作区 `revision` 并锁定并发写入；该模式不挂载普通 `/v1/executions` 提交路由，查询、取消和信号投递沿用本文的执行契约。

## OpenAPI 契约

`GET /openapi.json` 返回独立执行 API 的 OpenAPI 3.1 描述，包含 `submitExecution`、`getExecution`、`cancelExecution`、`signalExecution` 操作及 `Execution`、`Frame` 和错误模型。读取 Schema 不连接 Temporal，也可通过命令行静态导出同一份定义：

```sh
uv run --locked python -m axcutor_playground.execution_http --output openapi.json
```

`input`、`frames[].variables` 和 `result.output` 承载无损业务 JSON；生成的客户端类型不能替代任意精度数值与有序对象处理。

完整工作台的 Schema 由 `axcutor_playground.http` 定义；修改后端 HTTP 定义后，可通过 `npm --prefix playground run generate:client` 同步更新 `playground/openapi.json` 与前端客户端。

## Python 宿主接口

协议无关的执行管理由 `axcutor_host.AxcutorHost` 提供（异常基类为 `HostError`，与 CLI 和 HTTP 框架解耦）。它仅持有 Temporal Client、任务队列与运行选项覆盖表，通过任务队列提交工作流，不持有 Worker 实例或执行缓存：

```python
from axcutor_host import AxcutorHost

# client 为 Temporal Client；entry、mounts 和 inputs 使用下文的提交契约
host = AxcutorHost(client, task_queue="axcutor")
execution = await host.submit(entry, mounts, inputs)
final = await host.wait(execution["execution_id"])
```

`get`、`wait`、`cancel`、`signal` 在单次操作中固定目标执行轮次，保留协同取消与跨子工作流信号路由语义。取消调用方的 `wait` 任务仅停止本地观察，不会取消远端工作流。

## 提交执行

```http
POST /v1/executions
Content-Type: application/json
```

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `execution_id` | string，可省略 | 非空执行 ID；省略时生成 UUID。保留期内相同 ID 返回 `409`，不复用已有执行。 |
| `entry` | string | `__main__` 或对应根 `packages` 中各段均公开的绝对点分包名，如 `billing.charge`。 |
| `mounts` | object | 根名到挂载单元的映射；每个单元包含 `packages`、`resources` 和可选 `allowed_executors`。 |
| `input` | object，可省略 | 默认 `{}`；字段集合和类型必须符合入口输入契约。 |

局部字段示例：

```json
{
  "entry": "__main__",
  "mounts": {
    "__main__": {
      "packages": {
        "__main__": { "format": "yaml", "text": "..." }
      },
      "allowed_executors": ["signal"]
    }
  },
  "input": { "n": 7 }
}
```

`text` 应为完整合法工作流文档，示例中的 `"..."` 仅表示源码位置。`use: workflow:billing.verify` 精确引用 `mounts["billing"].packages["billing.verify"]`；禁止文件路径、扩展名、相对引用与 `workflow:__main__`。根包名必须公开，每个以 `_` 开头的私有段限制调用方位于其紧邻父包子树内。

挂载单元内字段均可省略：`packages` 和 `resources` 默认为 `{}`，值均为完整点分包名到 `{format, text}` 源文档的映射，`format` 为 `"yaml"` 或 `"json"`。每个文档必须归属当前挂载根，违反时报 `schema.package_path`；非法根名报 `schema.mount_root`。`__main__` 单元只能包含同名工作流，不能声明资源头文件。重复 JSON 键在解析时拒绝。

`allowed_executors` 为 `null` 或省略时不限制；显式字符串列表仅允许其中的 `run.executor`（列表项须为合法标识符），空列表 `[]` 禁止全部 `run`。策略仅作用于当前挂载根下的工作流（含内联子图节点），跨根调用按被调方所属根的策略检查；违规报 `schema.executor_not_allowed`。确定性 `builtin:*` 算子不受此策略限制。

未被入口调用闭包引用的工作流不解析正文，但所有挂载文档路径与 `__ax_resources__.yaml` 资源声明均会检查；工作流递归引用未声明的 `handle` `kind` 报 `type.unknown_resource`。所有静态编译错误均返回 `422 compilation_failed`（含匿名入口 `"__main__"` 在根 `input` / `output` 中使用无公开 `locator` 句柄时的 `type.root_handle`）。具名工作流包允许保留内部句柄接口供跨包调用，但当其作为宿主根 `entry` 提交且顶层 `input` / `output` 含无公开 `locator` 句柄时，根接口预检抛出 `InputValidationError` 并返回 `422 input_validation_failed`，不创建执行。

具名包中的 `module` 节点绑定 Worker 已安装的 Python 实现；提交请求不包含 Python 源码或 Worker 模块挂载映射。匿名 `__main__` 不能直接使用 `module`，但可调用公开具名包。已提交的工作流源码快照不随磁盘文件变动而改变，Python 依赖部署见 [04. 包挂载与 Python 模块](../guide/01-foundations/04-packages-and-modules.md)。

成功返回 `202 Accepted`，附带资源路径 `Location: /v1/executions/{execution_id}` 和初始 `Execution`（短工作流在响应前完成时可直接返回终态快照）。提交会通过有期限的活跃查询确认 Worker 能响应，无响应时返回 `503`；后端已接受的执行不会因查询超时被自动撤销，调用方可显式指定 `execution_id` 并在收到不确定的 `503` 后查询该 ID。

`Location` 对执行 ID 进行 URI 编码（包括点号，如 ID `..` 编码为 `/v1/executions/%2E%2E`）。客户端应直接使用返回的 `Location` 查询，或在其后直接追加 `:cancel` / `:signal` 发起取消或信号投递，无需预先解码路径。

## 查询与取消

```http
GET /v1/executions/order-001
POST /v1/executions/order-001:cancel
```

查询成功返回 `200`。运行态需要可响应的 Worker；终态直接从执行历史读取，不要求 Worker 在线。

取消请求触发协同取消及后代执行器的 `abort_signal`。清理尚未结束时返回 `202` 和运行态资源；取消完成或已处于任意终态时返回 `200`。重复取消保持终态，取消不会升级为后端强制终止。查询、结果读取和取消在单次操作内固定同一执行轮次，避免同名执行变动造成状态混用。

## 投递外部信号

```http
POST /v1/executions/order-001:signal
Content-Type: application/json

{"address":"__main__/decision","branch":"approved","output":{"reviewer":"alice"},"step":2}
```

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `address` | string，必填 | 非空，直接采用当前 `Frame.waiting` 中的地址。 |
| `branch` | string 或 null，可省略 | 非空分支名；节点声明 `branches` 时必须选择合法分支，无分支节点省略或传 null。 |
| `output` | object，可省略 | 默认 `{}`；显式 null、非对象均为 `400`。字段与类型须满足目标节点或所选分支的输出契约。 |
| `step` | integer 或 null，可省略 | 提供时须为正整数，且等于等待槽位所属帧当前 `steps`，否则 `409`。用于避免循环中迟到或重发信号唤醒下一轮。 |

请求对象闭合，未知顶层字段返回 `400 invalid_request`。信号契约错误返回 `422 signal_validation_failed`，原栈帧的等待状态、变量和步数保持不变，不消耗节点重试预算。有效输出按节点契约过滤顶层多余字段后恢复控制流。

当目标 `signal` 节点的输出包含声明了公开 `locator` 的 `handle` 字段时，按外部闭合信封传入定位符；若该定位符对应的本执行内部资源正处于外部删除途中，同样返回 `422 signal_validation_failed` 并保持节点等待。

已接受且根执行进入终态时返回 `200`，仍处于运行态或停在后续等待点时返回 `202`，响应体均为根 `Execution`。已结束、未处于等待的地址或 `step` 不匹配返回 `409 signal_not_waiting`。内部独立 Child Workflow 的拆分层数和后端执行 ID 对调用方透明，宿主按逻辑拓扑地址递归定位并固定执行轮次。

投递超时或连接故障返回 `503 runtime_unavailable`，此时信号可能已生效；调用方应先查询同一执行的 `waiting`、`steps` 与变量，避免盲目重发。根单步帧及直接迭代帧的 `steps` 为 `0` 时应省略 `step`；`step` 比对所属帧当前计数，同帧其他并发节点激活可能推进该计数，节点内部超时重试不增加计数。

## Execution 资源

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `execution_id` | string | 对外执行标识，不暴露内部 run ID。 |
| `status` | string | `running`、`completed`、`failed`、`cancelled`。 |
| `started_at` | string | 后端记录的 UTC ISO 8601 启动时间。 |
| `finished_at` | string 或 null | 终态结束时间；运行态为 null。 |
| `frames` | Frame[] | 活跃树；全部终态为 `[]`。 |
| `result` | object 或 null | 仅 completed 非空，含 `branch` 和 `output`。 |
| `error` | object 或 null | 仅 failed 非空，含 `category`、`type`、`message`。 |

终态局部示例：

```json
{"status":"completed","frames":[],"result":{"branch":null,"output":{"n":7}},"error":null}
```

`branch` 为根出口分支名，无分支默认 `end` 为 `null`；`output` 为该出口承诺产出的变量对象。运行态的 `result`、`error`、`finished_at` 均为 `null`；取消终态的 `result` 与 `error` 均为 `null`。

`error.category` 为 `recoverable` 时，`type` 为 `execution`、`timeout`、`contract`、`max_steps`；`category` 为 `panic` 时，`type` 为 `StackOverflowError`、`MaxStepsExceededError`、`EngineError`。后端外部强制终止、执行级超时及未知基础设施故障映射为 `panic / EngineError`。

## Frame 活跃树

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kind` | string | `function`、`block`、`iteration`。 |
| `address` | string | 根为入口工作流标识；后代为调用路径，可包含迭代 `[index]`。 |
| `cursor` | string | `start`、正在执行的节点 ID、节点出口 `id.branch`；根 run 为 `run`。 |
| `waiting` | string[] | 当前帧直接管辖的待认领 `signal` 地址，按注册顺序排列，无等待时为 `[]`。 |
| `call_depth` | integer | 根为 1；函数和块调用加 1，迭代帧不增加。 |
| `steps` | integer | 当前图累计激活步数。 |
| `max_steps` | integer 或 null | 图步数护栏；根 run 与 iteration 为 null。 |
| `variables` | object | 当前帧中已绑定变量；未绑定变量不出现。 |
| `children` | Frame[] | 在途函数、内联图与并发迭代的后代帧。 |

例如，迭代下的子调用地址可为 `__main__/batch[0]/invoke`（或 `billing.charge/batch[0]/invoke`）。内部帧号、父帧号和后端子执行 ID 不进入 HTTP 资源。移交到后端独立子工作流的调用通过只读查询拼接回同一拓扑路径；树的不同层级在查询期间分别读取，不承诺跨后端执行的原子内存快照。若根执行在查询期间结束，最终响应采用终态并清空 `frames`。

`waiting` 列表可同时包含多个同帧并发挂起节点；根单步 `signal`（地址 `__main__`）与图内名为 `run` 的 `signal` 子节点通过精确地址区分：

```json
{"cursor":"run","waiting":["__main__/run"],"steps":1}
```

`iteration` 帧的 `variables` 包含父帧锁定快照中的已绑定变量及当前 `item`、`key`、`index` 注入项。该只读观察视图不扩大实际执行输入：执行器和内联块仍仅接收其显式声明的读取集合及适用注入项。

## 资源句柄表示与所有权边界

受管资源句柄（见 [05. 资源句柄](./05-resource-handles.md)）仅在静态 `TypeSpec` 为 `handle` 的位置采用闭合信封编码，普通业务对象不按键名猜测转换。

声明了公开 `locator` 的句柄（根 `input`、`result.output`、`:signal` `output`、`frames[].variables` 与调用轨迹同构）：

```json
{"$handle":"infra.storage:File","locator":{"file_id":"f_001"}}
```

未声明公开 `locator` 的内部句柄（仅只读脱敏出现在 `frames[].variables` 与调用轨迹中）：

```json
{"$handle":"infra.postgres:Conn"}
```

* **外部与内部隔离**：内部运行期 `rid` 与未公开的恢复定位符绝不出现在 HTTP 响应中；单键诊断对象 `{"$handle": "<kind>"}` 不能作为根 `input` 或 `:signal` `output` 提交。
* **所有权与清理**：经根 `input` 或 `:signal` 传入的资源由宿主持有外部实体删除权；工作流内部新建的资源仅在根执行 `completed` 且包含于 `result.output` 时移交宿主（`failed` 或 `cancelled` 不发生移交）。无论外部持久实体归谁所有，工作流结束时均会驱逐 Worker 内存缓存并关闭本地连接，但不删除归宿主持有的持久实体；通过 `frames` 或 `trace` 观察内部句柄不会改变其所有权。
* **访问授权**：HTTP 与内核预检仅校验名义 `kind` 与 `locator` 的静态值结构，不隐含跨租户访问授权；资源归属校验由宿主网关在提交 / `:signal` 入口执行，或由业务资源类在 `restore(locator)` 中验签。

## 无损 JSON

请求与响应保留任意精度整数、十进制数类别及对象插入顺序，不经二进制浮点中转。例如 `1` 为 `integer`，`1e0` 仍为 `number`（可规范化输出为 `1.0`）：

```json
{"integer":99999999999999999999999999999999999,"number":1.0,"ordered":{"z":3,"a":1}}
```

重复对象键、`NaN` 和 `Infinity` 被拒绝。客户端应使用支持无损数值的 JSON 实现读写 `input`、`result.output` 和 `frames[].variables`。

## 错误响应

全部非 2xx 响应采用统一结构，无附加诊断时 `details` 为 `null`：

```json
{"error":{"code":"input_validation_failed","message":"input violates the entry contract","details":null}}
```

| HTTP 状态 | code | 场景 |
| --- | --- | --- |
| 400 | `invalid_json` | JSON 语法错误、重复键、非有限数值或非法 UTF-8。 |
| 400 | `invalid_request` | 必填字段缺失、类型不符、额外字段或非法入口工作流标识。 |
| 404 | `execution_not_found` | 执行不存在或不是 Axcutor 根执行。 |
| 409 | `execution_already_exists` | 执行 ID 冲突，包括已结束执行。 |
| 409 | `signal_not_waiting` | 执行已结束、目标地址未等待或 `step` 不匹配，包括投递期间的关闭竞态。 |
| 422 | `compilation_failed` | 静态编译失败；`details` 为 `{ "diagnostics": Diagnostic[] }`，保持编译器诊断字段。 |
| 422 | `input_validation_failed` | 外部初始输入未通过入口 `TypeSpec` 校验，或具名根接口的句柄缺少公开 `locator`；对应 `InputValidationError`，不创建执行。 |
| 422 | `signal_validation_failed` | 外部信号的分支或输出未通过目标契约校验，或定位符对应的本执行内部资源正在执行外部删除；节点继续等待。 |
| 503 | `runtime_unavailable` | 后端不可达、超时或运行态 Worker 无响应。 |

未知路径与不允许的 HTTP 方法同样返回该错误结构；宿主内部未预期异常返回 `500 / internal_error`。工作流创建后的运行期业务失败记录在 `Execution.status = "failed"` 中，查询该执行仍返回 `200`。

## Playground 工作区执行

完整工作台使用独立的工作区接口。源码与资源头文件通过 `PUT /v1/workspace` 保存；单次运行的 `input` 直接在顶部 Input 编辑区内联填写并随执行请求提交，不写入工作区文件：

```http
POST /v1/workspace/executions
Content-Type: application/json

{"revision":"<当前工作区 revision>","entry":"workflows/demo/main/__axcutor__.yaml","input":{"items":[3,1,2],"delay_seconds":5}}
```

* **请求校验与响应**：`revision` 必填且须匹配当前工作区版本，否则返回 `409 workspace_conflict`。`entry` 为工作区内的相对工作流源文件路径。`input` 可省略（默认 `{}`）；显式非对象返回 `400 invalid_request`，根接口或输入值不满足契约返回 `422 input_validation_failed`。提交时工作台同步刷新驻留 Worker 的模块挂载表，成功返回 `202`，包含 `execution`、本次快照 `source` 与最新 `workspace`。
* **入口路径归属与历史输入恢复**：提交响应与 `GET /v1/workspace/trace` 中的 `source` 包含必填字段 `entry_path`（提交时的相对文件路径）、`entry`（编译与运行时的逻辑工作流标识）和 `mounts`：
  ```json
  {"entry_path":"scratch/a.json","entry":"__main__"}
  ```
  前端按 `entry_path` 独立维护各入口的原始 JSON 输入草稿：未编辑时根据编译产物的入口 `input` 与 `resources` 自动生成递归默认骨架（公开 `locator` 句柄默认填充 `{"$handle": "<kind>", "locator": <默认定位符>}`），并在浏览器内通过 WASM 实时执行根接口与外部输入预检；页面刷新后，仅当当前选中入口的 `entry_path` 与最近一次执行的 `source.entry_path` 一致且草稿未被手动编辑时，才从根调用轨迹无损恢复输入文本。
* **保存解耦与只读锁**：修改 Input 不改变工作区 `dirty` 状态或 `revision`；点击运行仅在源码存在未保存改动时先调用 `PUT /v1/workspace`。在提交准备、运行及本地资源清理期间，源码与 Input 同步保持只读，并全程沿用无损 JSON 传输规则。
