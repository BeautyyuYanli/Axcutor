编译、CLI、HTTP 与 Playground 统一使用 `entry + packages`，替代 `sources` 和文件路径式引用。工作流通过绝对点分包名寻址；编译器检查公开入口及每层私有段，匿名 `__main__` 只能调用公开包，不能直接使用 `module`。

具名包以 `__axcutor__.yaml` 与 `__init__.py` 组织，通过 `--mount ROOT=DIRECTORY` 显式挂载。提交保存不可变工作流快照；Python 实现由宿主预装，Worker 可用 `--module-mount ROOT=PYTHON_PACKAGE` 配置映射，提交不能更改该映射。编译及契约生成不执行 Python 包初始化代码。

新增标准 `module` 执行器，仅接受 `.` 或包内单点相对路径。`axcutor contracts` 生成同级契约，`--check` 检查缺失或过期模板；SHA-256 覆盖归一化输入、输出、分支及声明顺序。运行前核验契约类型来源与 SHA；生成前拒绝共享实现契约不一致、模板覆盖实现以及包与文件冲突。

`builtin` 与 `module` 共用每次尝试的输入隔离、严格输出检查和任务清理。成功、异常、取消及超时退出均排空所拥有的任务，迟到结果不会写回图状态。契约违约归为 `contract`，导入及业务异常归为 `execution`；模块异常保留物理文件堆栈。Python 部署版本与导入缓存仍由宿主管理。
