编译请求统一为 `{entry, mounts}`。每个根对应一个 `MountUnit`，分别保存工作流文档 `packages`、资源声明 `resources` 和执行器白名单 `allowed_executors`。Python 编译与契约生成、CLI、HTTP 和 Playground 均采用该结构；原顶层 `packages` 请求已移除，调用方需要迁移。

每份文档必须属于所在挂载根；匿名根 `__main__` 仅容纳同名入口工作流，不能声明资源。编译器先登记所有挂载中的 `__ax_resources__.yaml` 资源，再编译入口的工作流依赖闭包。句柄类型必须引用已声明且可见的资源；未声明时报 `type.unknown_resource`。白名单仅约束所属根内的 `run.executor`：省略或 `null` 表示不限制，空列表禁止所有 `run` 执行器，违反时报 `schema.executor_not_allowed`；跨根调用分别遵循各自根的策略。

目录快照同时收集工作流和资源头文件，不导入 Python。每个物理子目录对应一个包路径段，扫描拒绝非法目录段、符号链接、包与模块歧义及重复声明键。快照形成后，编译使用其中保存的文本，不依赖源文件后续变化；同路径资源诊断通过 `target_kind: resource` 定位头文件。

资源类通过资源包的 `__init__.py` 公开导出，可以定义于该包自身或其单文件子模块。Worker 校验定义模块的所属包和导出类身份，拒绝跨包重导出；资源实例反向解析为同一逻辑 `kind`。运行期沿用 Worker 的模块挂载配置，不读取资源声明 YAML，也不增加执行器白名单状态。
