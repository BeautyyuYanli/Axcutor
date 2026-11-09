# 04. Execution HTTP API

`axcutor serve` 启动 HTTP 宿主，提交、查询、取消与信号投递接口统一返回 `Execution` 资源。源码闭包与初始输入在提交端同步编译、校验，通过后才创建执行。宿主不保存执行状态；重启宿主不会取消已提交的执行。

## OpenAPI 契约

`GET /openapi.json` 返回 OpenAPI 3.1 描述，包含 `submitExecution`、`getExecution`、`cancelExecution`、`signalExecution` 操作及执行、帧和错误模型。读取 Schema 不连接 Temporal。静态导出使用同一份定义：

```sh
uv run --locked python -m axcutor_host.http --output openapi.json
```

`input`、帧变量和输出为任意业务 JSON；生成的客户端类型不能替代无损 JSON 处理。[Web Playground](../guide/00-web-playground.md) 展示原始文本序列化与响应切片的集成方式。

## 提交执行

```http
POST /v1/executions
Content-Type: application/json
```

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `execution_id` | string，可省略 | 非空执行 ID；省略时生成 UUID。保留期内相同 ID 返回 `409`，不复用已有执行。 |
| `entry` | string | `__main__` 或 `packages` 中各段均公开的绝对点分包名，如 `billing.charge`。 |
| `packages` | object | 点分包名到 `{ "format": "yaml" 或 "json", "text": "源码" }` 的映射；应提供全部传递引用。 |
| `input` | object，可省略 | 默认 `{}`；字段集合和类型必须符合入口输入契约。 |

局部字段示例：

```json
{"entry":"__main__","packages":{"__main__":{"format":"yaml","text":"..."}},"input":{"n":7}}
```

`text` 应为完整合法工作流，示例中的 `...` 只表示源码位置。`use: workflow:billing.verify` 精确引用 `packages["billing.verify"]`；禁止文件路径、扩展名、相对引用与 `workflow:__main__`。根包必须公开，每个私有段都限制调用方位于其父包子树。未被入口闭包引用的文档不参与编译，但所有包名均检查合法性，重复 JSON 键会被拒绝。

具名包中的 `module` 节点绑定 Worker 已安装的 Python 实现；请求不包含 Python 文件或 Worker 挂载配置。匿名 `__main__` 不能直接使用 `module`，可调用公开具名包。执行的工作流快照不会随源码文件更新，Python 依赖部署由宿主管理，见 [包与模块指南](../guide/01-foundations/04-packages-and-modules.md)。

成功返回 `202 Accepted`，附带资源路径 `Location: /v1/executions/{execution_id}` 和初始 `Execution`。短工作流可能在响应前完成，响应可直接包含终态。提交会通过有期限的活跃查询确认 Worker 能响应；无响应时返回 `503`。后端已接受的执行不会因查询超时被自动撤销。调用方可指定 ID，并在收到不确定的 `503` 后查询该 ID。

`Location` 对执行 ID 进行 URI 编码，包括可能被路径规范化解释的点号。例如 ID `..` 对应 `/v1/executions/%2E%2E`。客户端应直接使用返回的 `Location` 查询，或在其后追加 `:cancel` / `:signal` 发起取消或信号投递，无需先解码路径。

## 查询与取消

```http
GET /v1/executions/order-001
POST /v1/executions/order-001:cancel
```

查询成功返回 `200`。运行态需要可响应的 Worker；终态直接从执行历史读取，不要求 Worker 在线。

取消请求触发协同取消及后代执行器的 `abort_signal`。清理尚未结束时返回 `202` 和运行态资源；取消完成或已处于任意终态时返回 `200`。重复取消保持终态，取消不会强制终止执行。查询、结果读取和取消在一次操作内固定同一执行轮次，避免同名执行变动造成状态混用。

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

请求对象闭合，未知顶层字段返回 `400 invalid_request`。信号契约错误返回 `422 signal_validation_failed`，原帧的等待状态、变量和步数不变，不消耗节点重试次数。有效输出按节点契约过滤顶层多余业务字段，再恢复正常控制流。

已接受且根执行进入终态时返回 `200`，仍运行或到达后续等待点时返回 `202`，响应体均为根 `Execution`。已结束、未等待的地址或步数不匹配返回 `409 signal_not_waiting`。内部 Child Workflow 的层数和实际执行 ID 对调用方透明，宿主按逻辑地址递归定位并固定执行轮次。

投递超时或连接故障返回 `503 runtime_unavailable`，此时结果未知，信号可能已生效。调用方应先查询同一执行的 `waiting`、`steps` 与变量，避免无条件重发。根单步帧及直接迭代帧的 `steps` 为 `0` 时省略 `step`；`step` 比较帧当前计数，同帧的其他并发节点激活也可能推进该计数，节点内部重试不改变计数。

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

`branch` 为出口分支名，默认 `end` 为 null。`output` 为该出口承诺的变量对象。运行态的 `result`、`error`、`finished_at` 均为 null；取消终态的 `result` 与 `error` 均为 null。

`error.category` 为 `recoverable` 时，`type` 为 `execution`、`timeout`、`contract`、`max_steps`。`category` 为 `panic` 时，`type` 为 `StackOverflowError`、`MaxStepsExceededError`、`EngineError`。后端外部强制终止、执行超时及未知后端失败映射为 `panic / EngineError`。

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

例如，一个迭代下的函数地址可为 `__main__/batch[0]/invoke`（或 `billing.charge/batch[0]/invoke`）。内部帧号、父帧号和后端子执行 ID 不进入 HTTP 资源。移交到后端子工作流的调用通过只读查询还原到同一拓扑路径；树的不同部分是在查询期间分别观察的，不承诺跨后端执行的原子内存快照。根执行在查询期间结束时，最终响应采用终态并清空帧。

等待列表可包含多个同帧并发节点；根单步节点与名为 `run` 的图内节点也由精确地址区分：

```json
{"cursor":"run","waiting":["__main__/run"],"steps":1}
```

该片段表示图内节点 `run` 等待；根单步 `signal` 的等待地址则为 `__main__`。

iteration 的 `variables` 包含父帧锁定快照中的全部已绑定变量及当前 `item`、`key`、`index` 注入项。该观察视图不扩大执行输入：执行器和内联块仍只接收其显式声明的读取集合及适用的注入项。

## 无损 JSON

请求与响应保留任意精度整数、十进制数类别及对象插入顺序，不经二进制浮点中转。例如 `1` 为 integer，`1e0` 仍为 number，可规范化输出为 `1.0`。数值的等价文本写法可能规范化，数值及类别保持不变。

```json
{"integer":99999999999999999999999999999999999,"number":1.0,"ordered":{"z":3,"a":1}}
```

重复对象键、NaN 和 Infinity 被拒绝。客户端应使用支持无损数值的 JSON 实现读取 `input`、`result.output` 和 `frames[].variables`。

## 错误响应

全部非 2xx 响应使用以下结构，未提供附加诊断时 `details` 为 null：

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
| 422 | `compilation_failed` | 静态编译失败；details 为 `{ "diagnostics": Diagnostic[] }`，保持编译器诊断字段。 |
| 422 | `input_validation_failed` | 初始输入未通过入口 TypeSpec 校验。 |
| 422 | `signal_validation_failed` | 外部信号的分支或输出未通过目标契约校验，节点继续等待。 |
| 503 | `runtime_unavailable` | 后端不可达、超时或运行态 Worker 无响应。 |

未知路径与不允许的方法也返回 JSON 错误结构；宿主内部意外错误返回 `500 / internal_error`。工作流已创建后的业务失败属于 `Execution.status = failed`，查询仍返回 `200`。
