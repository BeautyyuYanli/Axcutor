---
name: run-workflow
description: 编写、提交和观察 Axcutor YAML/JSON 工作流，使用 axcutor CLI 或 Execution HTTP API。适用于本地源码闭包编译、执行、状态查询和协同取消。
metadata:
    namespace: system
---

# 运行 Axcutor 工作流

Axcutor 使用 YAML/JSON 声明强类型控制流图。先阅读项目的
`manual/docs/guide/` 和 `manual/docs/reference/`；运行接口见
`manual/docs/reference/04-execution-http-api.md`。

## 本地文件提交

在项目根目录安装宿主及锁定依赖，启动 Temporal 和 Worker：

```sh
# Docker 环境同时提供 Worker、HTTP API 与 Temporal Web UI。
docker compose up -d --build

# CLI 读取入口目录的源码文件，只编译入口的引用闭包。
docker compose run --rm cli run /examples/workflows/main.yaml --input /examples/inputs/demo.json
```

使用本机 Python 环境时：

```sh
uv sync --locked
# Worker 在独立终端持续运行。
uv run --locked axcutor worker
# 另一终端提交工作流；--source-root 决定 source ID 的根目录。
uv run --locked axcutor run examples/workflows/main.yaml --input examples/inputs/demo.json
```

`use: workflow:<path>` 精确引用 source-root 下的相对 POSIX 路径，
保留扩展名，不相对于调用文件解析。提交的是源码快照，执行时不读取本地文件。
默认等待执行结束；`--no-wait` 在提交后退出，已提交执行继续运行。
`--id` 指定执行 ID，省略时生成 UUID。默认等待模式输出初始及终态 Execution JSON 行；
终态的 `result` 包含 `branch` 和 `output`，completed 退出 0，failed/cancelled 非零退出。
编译、输入校验或提交失败在两种模式下均非零退出。`--no-wait` 只输出初始 Execution，
退出码仅反映提交是否成功；执行结果需使用返回的 execution_id 另行查询。

## HTTP 提交与观察

`uv run --locked axcutor serve` 默认监听 `127.0.0.1:8000`。
Compose 的 API 地址为 `http://localhost:8000`，可用
`AXCUTOR_HTTP_PORT` 修改端口。HTTP 宿主不启动 Worker。

- `POST /v1/executions`：提交 `entry`、`sources`、可选 `input` 和 `execution_id`；
  sources 将路径映射到 `{format: "yaml" | "json", text: string}`。
  同步编译及输入契约校验通过后启动，成功为 202，Location 指向执行资源。
- `GET /v1/executions/{execution_id}`：返回 running、completed、failed 或 cancelled，
  运行态包含活跃 frames；终态包含 result 或结构化 error，frames 为 []。
- `POST /v1/executions/{execution_id}:cancel`：请求协同取消。
  清理中返回 202，已在终态返回 200；不要将取消 ACK 当作清理完成。

所有入口复用 axcutor-host 的 ExecutionService，执行状态与历史由 Temporal 保存。
运行态查询要求 Worker 响应，终态读取不需要 Worker。无数据库或本地执行缓存。

## 数值与失败处理

使用 `axcutor_runtime.codec.dumps/loads` 或等价的无损 JSON 库传输载荷，
保留任意精度整数、十进制 number 类别及对象键序。拒绝重复键和非有限数值。

非 2xx HTTP 响应为 `{error: {code, message, details}}`：
400 为 JSON/请求形状错误，404 为执行不存在，409 为 ID 冲突，
422 为编译或输入校验失败，503 为后端不可达或 Worker 无响应。
编译错误的 details 保留 diagnostics。

ID 在 Temporal 保留记录期间不可复用，包括已结束执行。收到 503 时，
后端可能已经接受启动；使用原 ID 查询，不要直接改用新 ID 重复提交。
宿主或 CLI 退出不会自动取消执行。此入口执行受信任的 Python 内容，
不提供用户认证或隔离不受信任代码的沙箱。
