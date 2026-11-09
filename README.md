# Axcutor

Axcutor 将 YAML/JSON 工作流编译为自包含快照，通过 Temporal 执行强类型控制流图和 Python Activity。

## 启动文件工作台

```sh
# 克隆仓库后进入根目录；首次启动会构建 Python、WASM 与静态网页。
docker compose up
```

打开 [Playground](http://localhost:8080)，即可编辑真实 YAML/JSON/Python 文件、选择入口与输入文件、运行 module 工作流，并查看 CFG、变量探针和活跃帧。默认示例将 `[3,1,2]` 并发倍增为 `[6,2,4]`，汇总得到 `12`。

`examples/` 是只读模板；`playground/workspace/` 挂载到容器 `/workspace`。首次启动将示例初始化到 `playground/workspace/`，后续可在网页或宿主机直接编辑该目录下的文件，修改不被纳入 Git 跟踪。后端启动一个常驻 Worker，同一时间运行一个根工作流，图内仍可并发。提交、运行和清理期间，文件内容只读，新建、删除、保存与 Run 不可用；结束后可编辑并重跑，下一次 Run 重新导入当前 Python。多个页面共享 workspace，revision 冲突或运行忙时返回 `409`。详见 [Web Playground](manual/docs/guide/00-web-playground.md)。

## 使用 Docker CLI 执行工作流

```sh

# 编译并提交示例，输出执行 ID 和最终结果。
docker compose run --rm cli run /examples/workflows/demo/main --mount demo=/examples/workflows/demo --input /examples/inputs/demo.json
```

在 [Temporal Web UI](http://localhost:8233/namespaces/default/workflows) 查看执行历史、Activity 和输出。远程访问时，将 `localhost` 替换为运行 Docker 的主机地址。

命令输出初始和终态 `Execution` JSON；终态的 `result` 为：

```json
{"branch":null,"output":{"doubled":[6,2,4],"total":12}}
```

文件提交规则、实时查询和停止方式见 [Docker 运行指南](manual/docs/guide/00-run-with-docker.md)。语言教程与规范见 [文档中心](manual/index.md)。

HTTP API 默认地址为 `http://localhost:8000`，提供提交、查询和协同取消三个接口：

```text
POST /v1/executions
GET  /v1/executions/{execution_id}
POST /v1/executions/{execution_id}:cancel
```

请求字段、执行状态与错误码见 [HTTP API 参考](manual/docs/reference/04-execution-http-api.md)。本机安装使用 `uv sync --locked`，随后分别运行 `PYTHONPATH=examples/workflows uv run --locked axcutor worker` 和 `uv run --locked axcutor serve`。CLI 与 HTTP 均通过 `axcutor-host` 包中的同一执行服务完成编译、输入预检与状态查询；执行历史由 Temporal 保存。

具名工作流使用点分包名，例如 `demo.main`，对应含 `__axcutor__.yaml` 与 `__init__.py` 的目录。编译与 HTTP 请求使用 `packages` 映射；独立入口文档使用 `__main__`。预置 Python 模块执行器及契约生成方式见 [包与模块指南](manual/docs/guide/01-foundations/04-packages-and-modules.md)。

浏览器前端位于 `playground/`，独立 Python 后端位于 `playground/backend/`，提供 `axcutor-playground` 入口。本机执行 `uv sync --locked` 和前端生成、构建后，运行 `uv run --locked axcutor-playground`，即可在 `8080` 访问文件工作台。该包单向依赖 `axcutor-host/runtime/core`，负责 workspace、HTTP 与常驻 Worker 生命周期；`axcutor serve` 保留普通 Execution API。两个后端各自通过 `GET /openapi.json` 提供 OpenAPI 3.1 契约。
