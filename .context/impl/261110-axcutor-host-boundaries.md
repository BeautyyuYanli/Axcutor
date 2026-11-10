# Axcutor Host 职责拆分

Python 宿主接口统一为 `from axcutor_host import AxcutorHost`，提供 `submit/get/wait/cancel/signal`。`HostError`、执行模型、方法参数和业务错误码沿用既有契约；不提供 `ExecutionService` 兼容别名。

通用命令行由独立的 `axcutor-cli` 包提供，保留 `axcutor run`、`signal`、`contracts`、`worker`。host 和 CLI 可独立安装，不引入 HTTP 框架或 playground。CLI 负责参数、文件输入输出和进程信号接线，执行管理调用 host，契约生成和 Worker 组装调用 runtime。

独立执行 HTTP API 使用 `axcutor-playground api` 启动，替代 `axcutor serve`，默认监听 `127.0.0.1:8000`。它通过惰性 Temporal Client 提交和控制执行，不创建工作区或 Worker；Temporal 不可用时仍可读取 OpenAPI，并在联系后端前拒绝非法请求。HTTP 路径、请求与响应结构、无损 JSON、错误映射及执行 ID 编码保持既有契约。

不带子命令的 `axcutor-playground` 启动完整工作台，默认监听 `127.0.0.1:8080`。工作台继续持有专用队列上的驻留 Worker，通过工作区执行接口提交，并保持 revision、busy、源码快照、业务代码导入与清理约束。普通执行提交接口不挂载到工作台，查询、取消和信号沿用执行 API 的契约。

Worker 的创建、轮询和资源清理由 runtime 提供；普通 Worker 由独立 CLI 进程运行，工作台 Worker 由工作台应用生命周期管理。退出信号继续触发优雅关闭并等待在途 Activity 清理。`AxcutorHost` 只持有 Temporal Client、队列和运行配置，通过 Temporal task queue 提交工作流，不持有 Worker 实例。

Temporal 继续保存执行历史、状态和结果；host 与独立 API 不增加执行缓存。取消等待只停止调用方观察，不取消工作流；重启独立 API 不取消已提交执行。工作台的工作区文件和进程内最近一轮 Trace 仍由工作台独立拥有，其关闭取消与清理行为保持原有约束。

独立 API 的连接和监听选项 `--address/--namespace/--host/--port` 可位于 `api` 前后，重复配置采用后值。显式参数优先于环境变量，默认连接配置继续读取 `TEMPORAL_ADDRESS`、`TEMPORAL_NAMESPACE`，队列读取 `AXCUTOR_TASK_QUEUE`。镜像同时安装 CLI 与 playground，独立 API 服务使用新的 playground 命令入口。
