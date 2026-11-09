依据 RFC-0017，新增闭合执行器 `run: {executor: signal}`，在 Workflow 内等待外部消息，不调度 Activity。`output` 与 `branches` 定义恢复契约；`timeout_ms` 限制单次等待，省略时可无限期等待。重试和捕获仅处理 `timeout`，等待、非法投递及内部重试不额外计步。

`Frame.waiting: list[str]` 暴露待认领地址。HTTP `POST /v1/executions/{execution_id}:signal` 与 CLI `axcutor signal` 支持地址、输出、可选分支及 `step` 乐观锁；宿主固定执行轮次并递归路由 Child Workflow，成功后返回根 Execution。请求结构错误返回 400，未等待或步数不符返回 409，输出契约错误返回 422；投递结果未知时返回 503，调用方应先查询状态再决定是否重发。Playground 同步提供投递接口和等待地址展示。

同步 Validator 只读校验请求，合法信号由 Handler 原子认领。非法投递不改变等待状态、变量或步数，也不触发节点契约异常。拒绝请求不产生 Update Accepted/Completed 事件，但 Temporal 仍可能记录 Workflow Task 事件。跨边界数据使用无损 JSON 文本，保留任意精度数值和对象键序；宿主不新增执行存储，等待状态由 Temporal History 重放重建。

每次尝试拥有独立等待槽位，成功、超时及取消均负责清理。认领后直接推进控制流，不等待初始化 Timer；已认领结果不会被同轮节点期限改判为超时。并发输出仍经过原有屏障提交，外部取消和 Fail-Fast 继续展开清理，`for_each` 等待期间持有并发许可。

针对当前 SDK 的[冷 Update 初始化顺序问题](https://github.com/temporalio/sdk-python/issues/2080)，必要时创建一个由 Workflow Run 持有的后台初始化 Timer，每个 Run 至多一个；循环、重试及并发等待共享，Child Workflow 独立管理。已有可靠持久化边界时复用，Run 退出时取消并回收该 Timer。1 毫秒仅为名义时长，不承诺实际触发延迟；必要 Timer 会增加历史事件，信号恢复不依赖其触发时间。
