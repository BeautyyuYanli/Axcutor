# 使用 Docker 执行工作流

仓库根目录的 Compose 配置提供本地开发环境：Temporal 开发服务器、内置 Web UI、Axcutor Worker 和 HTTP API。Temporal 使用 SQLite 数据卷保存执行历史；Axcutor 宿主与 Worker 不维护独立数据库或执行状态缓存。

## 启动并执行示例

需要可用的 Docker Engine 和 Docker Compose。在仓库根目录执行：

```sh
# 首次启动时编译 Rust 原生扩展，并安装锁定的 Python 依赖。
docker compose up -d --build

# 提交端读取源文件、编译完整依赖闭包，再将快照提交给 Temporal。
docker compose run --rm cli run /examples/workflows/main.yaml --input /examples/inputs/demo.json
```

命令首先输出初始 `Execution`，随后等待并输出终态 `Execution`，两者都包含 `execution_id`。终态对象的 `result` 为：

```json
{"branch":null,"output":{"doubled":[6,2,4],"total":12}}
```

入口工作流对 `[3,1,2]` 以并发度 2 调用子工作流，按输入顺序汇集结果，再执行 Python 汇总 Activity。每个迭代 Activity 等待 5 秒，便于观察运行状态。工作流提交后，修改源文件不会改变该次执行使用的快照。

## 在 Web UI 查看状态

打开 Temporal Web UI：`http://localhost:8233/namespaces/default/workflows`。远程访问时，将 `localhost` 替换为 Docker 主机地址；UI 端口可通过 `TEMPORAL_UI_PORT` 环境变量调整，默认 `8233`。

在 `default` namespace 的执行列表中选择命令输出的 `execution_id`：

- 执行状态从 `Running` 变为 `Completed`，失败时显示 `Failed`。
- History 中的 Activity 事件展示输入快照、调度、完成结果或失败原因。
- 示例的子工作流调用在同一个解释器内执行，因此迭代 Activity 出现在入口执行的 History 中。
- `builtin:*` 确定性算子在 Workflow 内存中执行，不单独产生 Activity 事件。
- 使用 `get_runtime_snapshot` Query 查看活跃栈帧、游标、步数和 `env_json`; 完成后返回 `{"frames":[]}`。

也可直接通过 Temporal CLI 查询：

```sh
# 将 <workflow-id> 替换为提交命令输出的 execution_id。
docker compose exec temporal temporal workflow query --workflow-id <workflow-id> --type get_runtime_snapshot
```

## 提交自己的工作流包或入口文件

`cli` 服务默认将仓库的 `examples/` 只读挂载到 `/examples`。外部目录可额外挂载：

```sh
# 将本机目录挂载为 /workflows，并提交其中的入口与输入文件。
docker compose run --rm -v "$PWD/my-workflows:/workflows:ro" cli run /workflows/main.yaml --source-root /workflows --input /workflows/input.json
```

在统一工作流包模型下，宿主挂载的根包目录（如 `<source-root>/billing`）中任何包含 `__axcutor__.yaml`（或 `__axcutor__.json`）的包目录均按点分逻辑包路径（`PackagePath`）注册；独立单文件入口则以保留匿名标识 `"__main__"` 编译，仅入口及其 `use` 引用闭包参与编译：

```yaml
# 对应挂载根下的 billing/verify/__axcutor__.yaml 工作流包，按点分包路径引用。
use: workflow:billing.verify
```

输入文件必须是 JSON object，省略 `--input` 时使用 `{}`。命令在联系后端前完成静态编译和入口输入契约校验，使用运行时 JSON codec 保留数值精度和对象键序。编译、输入校验或提交失败时返回非零退出码。默认等待模式还按执行终态返回退出码：`completed` 为零，`failed` 或 `cancelled` 为非零。

提交成功后的等待通过执行历史长轮询完成，不反复查询活跃帧；Worker 暂时离线时继续等待其恢复，后端连接错误仍作为宿主错误返回。

如需提交后立即退出，使用 `--no-wait`；本地提交进程结束不会取消已经提交的执行：

```sh
# 每次执行默认生成新的 ID；--id 可指定便于搜索的 ID。
docker compose run --rm cli run /examples/workflows/main.yaml --input /examples/inputs/demo.json --id my-demo --no-wait
```

`--no-wait` 只输出初始 `Execution`，退出码仅反映提交是否成功，不反映执行终态；执行结果需使用返回的 `execution_id` 另行查询。相同 ID 在 Temporal 保留执行记录期间不可重复提交，已完成的执行也会冲突。

## 通过 HTTP 提交、查询与取消

`api` 服务在 `http://localhost:8000` 提供接口；可用 `AXCUTOR_HTTP_PORT` 调整宿主端口。以下请求包含一个完整源码快照，工作流将输入整数复制到输出：

```sh
# 源码以字符串传入；只编译入口及其 use 引用闭包。
curl -i http://localhost:8000/v1/executions \
  -H 'Content-Type: application/json' \
  -d '{"execution_id":"http-demo","entry":"__main__","sources":{"__main__":{"format":"yaml","text":"input:\n  n: {type: integer}\noutput:\n  n: {type: integer}\nrun:\n  executor: builtin\n  content: return Output(n=input.n)\n"}},"input":{"n":7}}'

# 查询终态输出或当前活跃帧；取消操作等待执行器协同清理。
curl http://localhost:8000/v1/executions/http-demo
curl -X POST http://localhost:8000/v1/executions/http-demo:cancel
```

提交成功返回 `202` 与资源 `Location`。取消仍在清理时返回 `202`，已处于终态时返回 `200`。Worker 无响应或后端不可达时返回 `503`；若启动请求已被后端接受而后续查询超时，执行仍可能存在，应使用原 ID 查询，不能将 `503` 当作执行未创建。接口字段、无损 JSON 规则和完整错误契约见 [HTTP API 参考](../reference/04-execution-http-api.md)。

此开发环境允许提交受信任的 Python `run` 内容，不提供认证与执行沙箱隔离，应在受信任网络中使用。

## Worker 生命周期与数据保存

`worker` 服务持续轮询 `axcutor` task queue，解释 workflow 并执行受信任的 `run.executor: builtin` 与 `run.executor: module` Python 代码。接到停止信号后，Worker 最多等待 20 秒让 Activity 完成，再按 Temporal 协议关闭；Compose 的停止宽限期为 30 秒。

```sh
# 查看基础设施和 Worker 状态。
docker compose ps
docker compose logs --tail=100 worker

# 重启 Worker 后，未完成的工作流可继续调度和重放。
docker compose restart worker

# 停止并移除容器，保留 temporal-data 数据卷和执行历史。
docker compose down
```

Temporal 的执行与历史数据保存在 `temporal-data` 命名卷中；`docker compose down -v` 会删除该卷。该配置用于本地开发；Temporal 开发服务器的运行选项见 [官方命令参考](https://docs.temporal.io/cli/command-reference/server)。

在宿主机运行时，可使用同样的命令行入口：

```sh
# 需要本机 Rust 工具链；只启动 Temporal 基础设施。
docker compose up -d temporal
uv sync --locked

# 在独立终端持续运行 Worker，另一个终端提交工作流。
uv run --locked axcutor worker
uv run --locked axcutor run examples/workflows/main.yaml --input examples/inputs/demo.json

# 可在另一个终端启动 HTTP 宿主，默认仅监听 127.0.0.1:8000。
uv run --locked axcutor serve
```

`worker`、`run` 与 `serve` 均接受 `--address`、`--namespace`、`--task-queue`，默认分别读取 `TEMPORAL_ADDRESS`、`TEMPORAL_NAMESPACE`、`AXCUTOR_TASK_QUEUE`，未配置时使用 `localhost:7233`、`default`、`axcutor`。`serve --host 0.0.0.0 --port 8000` 可调整 HTTP 监听地址与端口；HTTP 宿主不启动 Worker。
