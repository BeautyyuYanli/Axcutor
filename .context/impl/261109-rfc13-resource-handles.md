RFC-13 为内部工作流增加名义类型 `handle`，公开信封仅含 `$handle` 和 `kind`。句柄支持可空性与容器组合，禁止非空字面量伪造；根工作流的输入、输出及 signal 输出递归保持纯值，宿主 JSON 协议不变。

Python 公开 `Handle[T]`、`@resource` 和 `resources.put/get`。资源类决定定位、恢复与析构；put 完成协议校验后接管实例，get 仅允许当前 Attempt 获准的资源。生成契约使用精确资源类注解，`module_mounts` 和 CLI 的 `--module-mount` 决定 Python 导入，不改变逻辑 kind 或契约指纹。

Run Registry 保存身份、所有权、驻留与清理责任，Worker Store 保存真实实例，Attempt 负责成功交接前的新分配。内联调用共享 Registry；独立 Child 借入输入，完整移交成功输出中的新资源后再由父 Run 映射。执行环境、待提交结果和在途输入决定可达性，最后引用消失即安排回收；诊断与 trace 不保活资源。

复合 Worker 共享 Store 并监听共享队列和专属队列。瞬态资源必须回原 Worker；可恢复资源可在 Sticky 排队超过 5 秒后回退共享队列。非持久资源只清理已有实例，持久资源可恢复后幂等删除外部对象。取消排空与本轮清理共用 30 秒上限；未实际排空的本地使用者继续保护实例，析构失败保留责任。业务结果在业务和契约校验完成时确定，Server 硬超时仍为原预算 A；幂等、迟到副作用安全与外部对象 TTL 由业务保证。

Activity 内部统一返回 `result/resources`，自定义 Executor 与根宿主结果仍为 `{branch, output}`。内部元数据增加 `default_queue`，记录原默认队列以保证离线重放路由一致，不进入公开句柄。Playground 在占用执行状态前校验根接口，确定性拒绝保留原运行记录并允许继续编辑和提交。
