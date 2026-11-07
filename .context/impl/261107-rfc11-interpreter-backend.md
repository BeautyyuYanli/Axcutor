# RFC 11 解释器后端实现

解释器直接消费成功编译的 `{entry, workflows}` bundle 与图内 `routes`，支持根 run、图、内联图、workflow 调用和全部 16 个 builtin。既有编译器和 WASM/Python 文本接口保持兼容。新增 Python 运行时最低支持 3.11，原生绑定仍支持 3.10。

`compile_bundle(entry, sources)` 返回 bundle JSON 文本；编译失败通过 CompilationError 保留原始结果文本和结构化诊断。`AxcutorInterpreterWorkflow.run` 接收 `{bundle_json, input_json, options?}`，返回 `{branch, output}` JSON 文本。Rust 通过 Python 文本接口统一提供参数求值、builtin 求值、输入与返回契约校验；返回值仅提交当前分支承诺的字段，深层类型违约产生 contract。入口输入必须完整、类型正确且没有额外参数。

业务载荷以 JSON 文本跨越 Temporal 边界，保留映射插入顺序、整数与浮点分类和非算术路径的数值精度。Python 内部使用保序 dict、任意精度整数与 Decimal，极值保留数字原文；数值不会变成 JSON 字符串。Rust 整数算术保持精确，number 算术使用 IEEE 754 双精度，非有限结果作为引擎错误终止。

每个图帧独立拥有环境、游标、步数与调用深度，未绑定以键缺席表示，null 保持已绑定。函数帧只接收显式参数，内联帧只接收捕获与迭代注入；各自叠加本图 default。Fork-Join 和 for_each 从共同快照读取，全部收敛后一次提交；失败、取消和迟到响应不提交局部结果。for_each 按源顺序汇聚，跳过缺席字段，空集合计零步。状态由 Temporal History 重放恢复，解释器不引入额外数据库。

节点支持 execution、timeout、contract、max_steps 的 retry/catch/fail；重试共用锁定输入，且不增加所属图激活步数。contract 可重试时由 Workflow 循环统一管理总尝试预算，每次 Activity 仅尝试一次；其余包含 timeout、排除 contract 且可精确表达的正固定退避策略使用原生 Activity RetryPolicy。深度超限、根图超步数、引擎错误和外部取消穿透 DSL 捕获。未声明 timeout_ms 的 run 使用可配置的宿主期限，默认七天；语义选项随请求固化进 History。

业务执行器为 Activity Worker 本地注册的异步函数，接收独立请求副本和进程内 AbortSignal。Activity 返回前与 Workflow 收到成功响应后均执行 Rust 契约校验。取消及超时时置位信号、取消并等待本地任务，退出时清理心跳、期限及取消监听任务。执行器负责协作取消、释放外部资源及业务幂等；解释器回滚不撤销外部副作用，Server 已宣告超时后也不能保证另一台 Worker 的资源已释放。

普通 workflow 调用使用函数帧；Temporal 建议新历史或达到显式 child_workflow_history_events 阈值时，在调用边界派生 Child Workflow，携带相同 bundle、选项及递增深度，并由 DSL 控制重试。Child 只隔离后续调用历史，不清空父历史。`get_runtime_snapshot` 返回当前 Workflow 的活跃帧树、游标、计步与 env_json 独立快照；帧退出后移除，Child 可单独查询。公开请求与执行器接入示例见 [运行时接口](../../docs/runtime-api.md)，设计依据见 [定稿方案](../proposals/261107-rfc11-interpreter-backend.md)。
