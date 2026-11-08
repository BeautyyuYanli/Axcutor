# 03. 预置 Python 模块执行器与包契约

## 编译请求与包边界

编译请求为闭合对象 `{entry, packages}`。`packages` 为有序点分包名到 `SourceDocument {format, text}` 的映射，完全替代文件路径映射；入口为公开包或保留匿名标识 `__main__`。映射重复键在解析时拒绝，禁止静默覆盖。

`PackageSegment = Identifier \\ R_py`，根段必须公开。包名禁止 `__axcutor__` 段与文件后缀。`use: workflow:<PackagePath>` 仅接受绝对点分包名，不能引用 `__main__`。

对目标中每个私有段，其父前缀为 owner。调用方必须等于 owner 或以 `owner + "."` 为前缀，所有私有段均需满足；失败诊断为 `visibility.private_workflow`，位置为 `use`。外部入口不能选择任意私有段下的包。

宿主以显式根挂载构建快照，拒绝重复根、重叠物理树与同名包定义。编译器仅解析入口依赖闭包；先收集完整顶层签名，再检查各实现，支持循环调用且不依赖映射顺序。该过程不导入 Python。

## 标准执行体

`RunSpec::Module {content}` 序列化为 `{executor: "module", content}`，仅允许这两个键；额外字段产生 `schema.unknown_field`。`content` 仅为 `.` 或 `.` 后接一到多个 `PackageSegment`，非法值在 `/run/content` 产生 `schema.run`。

匿名 `__main__` 中任何层级的 `module` 产生 `module.anonymous_package`。规范化原子根和图内 `run` 节点保存所属 `workflow_id`，内联图与迭代不会改变归属。解释器将该字段传入 Activity，业务模块导入只发生在 Activity。

## 契约生成与 SHA

`synthesize_contract_module` 复用 `builtin` 的冻结输入、严格输出、嵌套模型、浮点转换和 `_UNSET` 语义。模板无 `execute`；公开导出包括 `Input`、`Output` 或 `Branch`、`ReturnType` 与所有嵌套契约模型。

`compute_contract_sha256` 使用无损 codec 编码固定顺序的对象 `{input: input_schema, output: output_schema, branches: branches_contract}`，对紧凑 UTF-8 JSON 计算 SHA-256 小写十六进制摘要。字段与分支顺序保持编译器归一化顺序。摘要仅声明在模板模块级 `__axcutor_contract_sha256__`，不复制到模型类。

`.` 生成 `_contract.py`；`.a.b` 在本工作流目录内生成 `a/b_contract.py`。共享同一实际实现文件的引用必须具有相同契约，包括 `.` 与 `.__init__` 这类文件别名。模板目标不得与已有 Python 包目录同名，也不得覆盖实现文件。生成和 `--check` 均先完成全部规划与冲突检查；只有生成模式在检查通过后写文件，`--check` 只验证模板是否与当前契约一致。

## 运行期校验与所有权

Worker 固定逻辑根到 Python 根包的映射，默认同名。显式映射为封闭表，不允许提交请求注入或修改。`billing.charge + .verify` 唯一映射为 `<python_root>.charge.verify`；`.` 指向 `<python_root>.charge`。

加载后，`execute` 必须为协程函数。实现的 `Input` 必须是其生成模板的 `Input`，`Output` 或 `Branch` 必须属于同一模板，所有输出变体须为该模板严格模型。模板 SHA 与请求 SHA 相等后，才构造输入并执行业务。

输入来自独立深拷贝，输入及嵌套模型冻结。返回值必须为本契约输出模型或分支变体的实例；导出时剥离 `_UNSET`，再由 Rust `validate_run_contract` 校验分支、必填字段与深层类型。绕过 Pydantic 构造的非法值仍在 Rust 边界拒绝。

导入期异常和普通业务异常保持 Python 物理堆栈并归为 `execution`；异步入口缺失、契约类型或 SHA 失配、返回类型违约与该模板 `_StrictModel` 子类的 Pydantic 校验异常归为 `contract`。无关业务模型的校验异常保持 `execution`。

`builtin` 与 `module` 共用单次 Python 调用生命周期：构造隔离输入、竞速业务任务与取消信号、核验并导出输出，所有退出路径均取消并排空所拥有任务。源码合成或模块加载、契约来源检查与各自的错误位置诊断保留在执行器入口。外层 Activity 管理心跳、deadline 与最终 Rust 校验，超时和取消优先于迟到返回值。Python 导入缓存属于宿主进程，工作流快照只固定 DSL，不固定或携带 Python 实现版本。
