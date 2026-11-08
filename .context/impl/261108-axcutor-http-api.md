源码扫描、命令行与 HTTP 协议由独立包 `axcutor-host` 承担。`ExecutionService` 接收内存态源码和输入，在联系后端前同步完成编译与输入契约校验；CLI 与 HTTP 复用该服务，确定性运行时保持协议无关。

HTTP 提供 `POST /v1/executions`、`GET /v1/executions/{execution_id}` 和 `POST /v1/executions/{execution_id}:cancel`，统一返回 `Execution`。提交成功返回 202 与可往返特殊 ID 的 Location；取消清理中返回 202，终态返回 200。错误统一为 `{error:{code,message,details}}`，分别以 400、404、409、422、503 表示请求错误、资源不存在、ID 冲突、编译或输入违约、运行时不可用；编译错误保留结构化 diagnostics。

Temporal 保存编译后的源码快照、执行历史及结果，宿主不维护独立数据库或执行缓存。执行状态为 running、completed、failed 或 cancelled；终态帧树为空，成功包含出口分支与输出，失败包含 recoverable/panic 分类及具体类型。公开资源仅包括 Axcutor 根执行；同一 ID 在后端保留记录期间不可复用。

运行态帧树以拓扑地址和 children 表达调用与并发层级，隐藏内部帧号及后端标识。variables 保留全部已绑定变量；迭代观察视图包含父快照和注入项，执行器输入仍受声明的读取集合限制。移交至后端子执行的活跃帧也会还原到树中，但跨执行查询不保证原子快照。输入、输出及帧变量统一保留任意精度整数、十进制数类别和对象键序，拒绝重复 JSON 键与非有限数值。

`axcutor worker/run/serve` 分别承担 Worker、文件提交和 HTTP 服务。run 输出初始 Execution，默认通过固定执行轮次的历史长轮询等待并输出终态；失败或取消以非零退出。`--no-wait` 只输出初始状态，退出码仅反映提交是否成功。结束提交进程或取消等待不会取消持久执行；取消终态在后代完成协同清理后产生。serve 不启动 Worker，终态读取无需 Worker 在线。

短工作流可能在提交响应前完成，因此 202 响应可包含终态。后端接受启动后若 Worker 查询超时，提交仍可能返回 503；调用方应使用原 ID 查询，不能据此断定执行未创建。宿主重启不影响后端保存的执行。
