# Axcutor

Axcutor 将 YAML/JSON 工作流编译为自包含快照，通过 Temporal 执行强类型控制流图和 Python Activity。

## 使用 Docker 执行工作流

```sh
# 构建 Rust/Python 宿主，启动 Temporal、Web UI、Worker 和 HTTP API。
docker compose up -d --build

# 编译并提交示例，输出执行 ID 和最终结果。
docker compose run --rm cli run /examples/workflows/main.yaml --input /examples/inputs/demo.json
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

请求字段、执行状态与错误码见 [HTTP API 参考](manual/docs/reference/04-execution-http-api.md)。本机安装使用 `uv sync --locked`，随后分别运行 `uv run --locked axcutor worker` 和 `uv run --locked axcutor serve`。CLI 与 HTTP 均通过 `axcutor-host` 包中的同一执行服务完成编译、输入预检与状态查询；执行历史由 Temporal 保存。
