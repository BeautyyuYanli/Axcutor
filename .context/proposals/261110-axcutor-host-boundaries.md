# Axcutor Host 职责拆分

## 包边界与公共接口

保留 `axcutor_host` 包，将其收敛为协议无关的宿主侧执行管理接口。核心类命名为 `AxcutorHost`，实现位于 `host.py`，通过包根导出；异常基类继续使用 `HostError`。命令行入口独立为 `axcutor_cli` 包，全部 HTTP 适配与服务器入口归入 `axcutor_playground`。

```python
from axcutor_host import AxcutorHost

host = AxcutorHost(client, task_queue="axcutor")
execution = await host.submit(entry, mounts, inputs)
```

`AxcutorHost` 提供 `submit/get/wait/cancel/signal`，保留执行模型和协议无关异常。方法参数、Execution/Frame 结构、业务错误码、运行选项与执行语义沿用既有契约。仓库调用方、示例与现行文档统一使用 `AxcutorHost`，不提供 `ExecutionService` 兼容别名。

`submit` 接收入口标识、按根组织的 mounts、业务输入和可选执行 ID，先编译并完成入口与输入校验，再向 Temporal 提交工作流。`get/wait/cancel/signal` 保留执行轮次固定、协同取消和子工作流信号路由行为。HTTP 状态码、请求体校验与 OpenAPI 属于 playground 适配层。

依赖方向为：

```text
axcutor_cli ─────────┬──> axcutor_host ───> axcutor_runtime ───> axcutor_core
                    └──> axcutor_runtime
axcutor_playground ──┬──> axcutor_host
                    └──> axcutor_runtime
```

各包显式声明直接使用的依赖。host 与通用 CLI 不依赖 Starlette、Uvicorn 或 playground；runtime 不反向依赖 host、CLI 或 playground，并保持 Workflow sandbox 所需的导入隔离。

## Worker 生命周期与任务提交

普通部署由终端或进程管理器拉起 `axcutor worker` 进程。CLI 负责参数与环境变量解析、Temporal 连接、进程退出信号接线，调用 `axcutor_runtime.worker.create_worker` 运行 Worker。runtime 负责执行器注册、轮询器组装和资源清理；CLI 保留退出时等待在途 Activity 清理的行为。

工作流提交由 `AxcutorHost` 调用 Temporal `start_workflow`，通过 task queue 路由到轮询该队列的 Worker。host 仅持有 Temporal Client、队列与运行配置，不持有 Worker 实例。工作流内部的执行与资源管理继续由 runtime 承担。

完整工作台由 `Playground.start()/close()` 管理进程内的驻留 Worker。工作台为 Worker 和 `AxcutorHost` 配置同一个专用队列，协调工作区锁、业务代码导入、在途执行和关闭清理。

Temporal 继续持久化工作流历史、执行状态与结果。host 不新增执行记录或结果缓存；取消调用方的等待不取消工作流。工作台继续独立拥有工作区文件及进程内最近一轮源码、视图和 Trace。工作台关闭时的协同取消行为继续由工作台负责。

## 薄 CLI 与 HTTP 入口

`axcutor_cli.cli:main` 注册为 `axcutor` 命令，提供 `run`、`signal`、`contracts`、`worker`。CLI 负责输入输出和调用适配：挂载加载与契约生成调用 runtime，执行提交与控制调用 host。业务 JSON 使用现有无损 codec，保留等待模式、输出行数与退出码契约。

独立执行 API 通过 `axcutor-playground api` 启动，支持 Temporal 地址、namespace、task queue 和 HTTP 监听参数。该模式使用惰性 Temporal Client，不创建工作区或驻留 Worker，服务既有 `/v1/executions` 提交、查询、取消、信号接口与 `/openapi.json`。后端不可用时仍可读取 Schema，并在联系后端前拒绝非法请求。

`axcutor-playground` 默认启动完整工作台，沿用现有工作区、示例、静态目录及监听参数。工作区执行通过 `/v1/workspace/executions` 提交，继续经过 revision、busy、源码快照与清理约束；该模式不挂载普通 `/v1/executions` 提交路由，查询、取消和信号接口继续复用执行契约。

两个 HTTP 应用保留各自的创建函数和生命周期，共用必要的响应编码、错误映射、执行 ID 编码、执行模型 Schema 与信号控制器。请求与响应继续保留大整数、小数类别和对象顺序；错误状态、Location 编码及任意非空执行 ID 的路由行为保持一致。不新增通用控制器框架或 Worker 管理抽象。

## 目标文件结构

以下列出与本次拆分有关的文件，其余 runtime 和前端业务模块沿用现有结构。

```text
python/
├── axcutor_host/
│   ├── pyproject.toml
│   └── src/axcutor_host/
│       ├── __init__.py                 # AxcutorHost、执行模型和异常的公共导出
│       └── host.py                     # 执行管理与 Temporal 状态投影
├── axcutor_cli/
│   ├── pyproject.toml                  # 注册 axcutor 命令
│   └── src/axcutor_cli/
│       ├── __init__.py
│       └── cli.py                      # 四个 CLI 子命令及进程信号接线
└── axcutor_runtime/
    └── src/axcutor_runtime/
        ├── worker.py                   # Worker 组装与资源生命周期
        ├── workflow.py                 # 工作流解释执行
        ├── packages.py                 # 挂载与源码快照加载
        └── contracts.py                # 契约生成

playground/
├── backend/
│   ├── pyproject.toml                  # 注册 axcutor-playground 命令
│   ├── src/axcutor_playground/
│   │   ├── __init__.py
│   │   ├── cli.py                     # 独立 API 与完整工作台的服务器入口
│   │   ├── execution_http.py          # 执行 HTTP API 与执行 OpenAPI
│   │   ├── http.py                    # 工作区、Trace、静态资源及 ASGI 生命周期
│   │   ├── http_common.py             # 共用 JSON 响应、错误映射与 ID 编码
│   │   ├── coordinator.py             # 工作台执行与 Worker 生命周期协调
│   │   └── workspace.py               # 工作区文件存储、revision 与锁
│   └── tests/
│       ├── test_cli.py                # HTTP 启动入口
│       ├── test_execution_http.py     # 独立执行 HTTP 契约
│       ├── test_mounts.py             # HTTP 挂载与编译诊断契约
│       ├── test_playground.py
│       └── test_workspace.py
├── openapi.json                       # 通过现有工具生成
└── src/client/                        # 通过现有工具生成

tests/
├── host/
│   └── test_host.py
├── cli/
│   └── test_cli.py
└── runtime/
```

host 的执行模型和异常继续与 `AxcutorHost` 放在同一模块，不进一步拆分。现有 CLI、HTTP 和挂载测试按对应契约迁入目标目录，共享 Temporal fixture 继续位于 `tests/conftest.py`。HTTP 启动测试集中到 playground，CLI 测试使用真实命令入口。

## 项目配置与契约引用

根 `pyproject.toml` 纳入 `axcutor_cli` workspace 成员及来源声明，并同步开发依赖、类型检查路径和测试目录。CI 的 Python 检查范围对应新的目录布局。

Docker 镜像显式安装 CLI 与 playground，保持默认 `axcutor worker` 入口可用。Compose 的独立 API 服务使用 `axcutor-playground api`；完整工作台、普通 Worker 与工具 CLI 的职责维持分离。

更新公共导入、命令入口、模块路径与 HTTP Schema 导出方式的文档和引用。修改 OpenAPI 源定义后，使用现有生成命令更新 Schema 和客户端；依赖锁文件使用对应工具生成，不手工编辑生成文件。
