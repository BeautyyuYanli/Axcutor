# RFC 11 解释器后端实施方案

## 范围与规范依据

在现有 Rust 编译器和 Python 绑定之上，实现 RFC 11 的确定性 Temporal 解释器、业务 Activity ABI，以及解释器必需的 Rust 纯函数运行内核。执行语义以 `manual/spec/04-runtime-execution-model` 和 `05-executor-abi` 为准；归一化结构沿用 `docs/compiler-api.md` 和现有 `ast`，使用 `{entry, workflows}` 与图内 `routes`，不采用 RFC 示例中尚未落地的 `modules`、`edge_map` 别名。

实现覆盖根 run、根图、内联图、workflow 调用、全部 16 个 builtin、分支与 kill、Fork-Join、数组和 OrderedMap 的 for_each、精确计步、调用深度、retry/catch/fail、超时、协作取消、状态 Query，以及 RFC 的历史压力下 Child Workflow 调用边界。已有编译器、WASM 和文本绑定保持原有调用契约。

不引入独立 IR、代码生成、解释器数据库、Redis、通用插件框架或另一套 Python 类型检查器。业务执行器交付可注册的异步 Python 函数适配；HTTP、模型调用、脚本沙箱等通过同一 ABI 接入，不在核心中预置第三方业务 SDK。

## 对外接口与数值传输

Rust 核心增加纯函数入口及对应的 JSON 文本适配器，Python 扩展只转换文本和宿主错误：

```text
eval_arg(arg_json, env_json) -> value_json
eval_builtin(op, bindings_json, env_json, symbols_json) -> result_json
validate_run_contract(response_json, output_schema_json, branches_json) -> result_json
validate_input(input_json, schema_json) -> input_json
```

`eval_builtin` 返回 `{branch, output}`，output 已按绑定重命名为调用方变量；`symbols_json` 是当前图及迭代注入项的类型环境，用于按静态操作数类型决定 integer 与 number 运算。输入路径和常量使用既有 ArgValue。所有接口复用现有 TypeSpec、实例兼容和深度相等判定，不重新编译工作流。

`validate_run_contract` 返回标准化 `{branch, output}`，只保留所选分支承诺的字段，忽略其他顶层字段；缺字段、非法分支或深层类型不符返回契约错误。闭合 TypedDict 的多余属性仍然拒绝。`validate_input` 要求根输入完整且类型正确，拒绝未声明参数；它不是可被节点 catch 消费的业务执行故障。

Python 包 `axcutor_runtime` 提供：

```text
compile_bundle(entry, sources) -> bundle_json
AxcutorInterpreterWorkflow.run(request) -> result_json
AxcutorInterpreterWorkflow.get_runtime_snapshot() -> snapshot
create_worker(client, task_queue, executors, ...) -> Worker
```

`compile_bundle` 接收与现有编译请求一致的源快照，调用 Rust 编译器；编译失败携带原始结构化诊断，不启动执行。运行请求为 `{bundle_json, input_json, options}`，结果文本为 `{branch, output}`。options 保存宿主的 Activity 默认期限、心跳、调用深度上限和任务队列路由配置；这些配置随请求进入 History，重放期间不从进程环境重新读取。

Workflow 与 Activity 的业务载荷采用 JSON 文本，与已有 Python 绑定一致。Python 在内部以保序 dict、任意精度 int 和 Decimal 保存 JSON 值，使用支持 Decimal 原生 JSON 数字编码的成熟库。非算术路径不损失编译产物的精细小数和键序；number 算术按规范转换为 IEEE 754 双精度，integer 算术保持精确。JSON 文本跨 Temporal 边界无需自定义 PayloadConverter；Query 中的变量环境同样以 JSON 文本返回。

## Rust 纯函数运行内核

新增 `crates/axcutor-core/src/kernel`，保留零 I/O、零时钟和零可变全局状态。现有 AST 中需要被 JSON 适配器读取的类型补齐反序列化能力，不增加第二套 AST。

路径求值只读已绑定根变量；仅最终缺失的可选叶子规约为 null。未绑定根或非法中间路径属于调用方破坏已验证契约的引擎错误，不转换为 execution/contract 供业务恢复。

16 个 builtin 按现有枚举与规范逐一求值。集合更新均返回新值；map_set 覆盖保留原键位置、新键追加，map_delete 保持剩余顺序；字符串长度按 Unicode 码点；array_get 处理负下标和越界。相等比较复用精确 JSON 深度值相等，忽略对象键序，布尔不作为数值。数值大小比较保持精确输入比较，避免大整数被 f64 舍入后错误相等。

整数加减乘和取模复用 num-bigint，取模遵循除数符号的数学取模。number 加减乘与真除法使用 f64，并保持浮点 JSON 分类。除零按 some/none/default 路由；不能表示为有限 JSON 数字的数值结果属于不可恢复的引擎错误，不伪造 null、输出或可恢复异常。

unwrap/get/math 的缺失分支与 array_shift.empty 不提交对应输出；empty 同样不提交 rest。普通图由已编译路由应用 kill，for_each 的单元素结果通过输出缺席跳过汇聚，不删除父帧旧值。求值器不重复推导或返回 kill 集。

## 帧、所有权与控制令牌

每次执行持有一个只读 bundle 和一棵活跃帧树。每个图帧拥有 env、cursor、steps、max_steps、call_depth 及必要的诊断地址；未绑定用键缺席表示，显式 null 保持已绑定。帧只存在于 Workflow 确定性内存，恢复依赖 Temporal History 重放，没有第二套状态存储。

函数帧仅由显式输入与本图 default 深拷贝构成；内联块帧仅由 input 捕获、迭代注入和自身 default 构成；迭代帧在锁定父快照上叠加 item/key/index。私有变量、执行器原地修改和失败尝试均不能污染父帧。根、调用和内联图共享同一个图执行循环，以帧种类和宿主输出契约区分边界，无需平行维护三套解释器。

控制令牌从 start 开始，按 routes 查唯一出边，先应用 kill_vars，再处理目标。普通节点返回分支和增量，校验后一次写入帧；end 按宿主分支契约提取并校验输出；fail 抛 execution。workflow 调用先求值 with.input，目标输出校验后按 with.output 投影重命名，未映射输出丢弃。

根深度为 1；每进入 workflow 或内联 graph 增加 1，默认上限 64，兄弟调用独立计量。每个图自持步数；普通节点每次激活加 1，retry 不额外计步；for_each 每获得并发配额并准备启动一个元素时加 1，空集合为 0。子图超步数在完成取消后向父边界抛 max_steps；根超步数与深度超限直接终止，不能被 DSL retry/catch 消费。

Query 返回活跃帧树、游标、步数及 env_json 的独立快照，不推进执行或修改状态。帧在 finally 中注销，失败和取消后不得留下活跃诊断节点。

## 节点状态机与 Temporal 错误边界

非 builtin 节点先锁定最小读取快照，然后在同一份快照上执行每次尝试。成功校验后提交；失败先完成局部取消与回滚，再按 retry.on 和次数判断重试；重试耗尽后按展开的 catch 表选异常分支并返回空增量，否则保留原选择器向父边界展开。

业务异常仅有 execution、timeout、contract、max_steps。引擎 Panic 与外部取消始终穿透；根入口把不可恢复引擎错误明确转换为失败的 Workflow，而非让 Temporal 不断重试 Workflow Task。解析器或内部断言错误不吞并为业务 execution。

run 分派到标准 `execute_run` Activity，请求包含 run_spec、最小 input_snapshot、output_schema、branches_contract，以及该次尝试需要的取消与计时信息。注册表和实际业务函数仅由 Activity Worker 持有，Workflow 内不调用业务函数或访问外部资源。

当 retry.on 包含 timeout、backoff_ms 为正且可被 Temporal 原生固定退避精确表达时，下推 Activity RetryPolicy；maximum_attempts 包含首次尝试、backoff_coefficient 为 1、不允许重试的 execution/contract 进入 non_retryable_error_types。未配置 retry 时 maximum_attempts 为 1。排除 timeout 或零间隔不能精确下推的策略使用 Workflow 层循环，每次底层 Activity 只尝试一次。列表与错误类型顺序固定，避免集合迭代造成重放差异。

节点声明 timeout_ms 时，run 使用 start_to_close_timeout，复合节点使用 Workflow 定时器包裹单次子图尝试，超时取消并等待该尝试的所有子任务收敛。Temporal 要求 Activity 总有有限期限；未声明 timeout_ms 的 run 使用随请求固化的宿主默认 Activity 期限，默认 7 天，此期限不从 DSL 推导。Worker 和调用方可显式配置，文档说明这一基础设施边界。

Activity 包装器先复制请求，将业务错误封装为 type=execution 的 ApplicationError；业务响应在返回 Server 前调用 Rust 契约校验，违约为 type=contract。Workflow 对 Activity 成功响应再校验一次，防止其他 Worker 破坏 ABI。ActivityError/ChildWorkflowError 的 cause 按明确类型展开，分别恢复选择器或不可恢复引擎错误，不用异常消息猜测分类。

## 并发、取消与 Activity 生命周期

Fork-Join 为各成员分配相同父快照和独立结果槽；for_each 为源集合分配保序槽并以 semaphore 控制活跃元素数。全部分支成功后才按声明顺序一次提交。for_each 跳过 catch 和未产出字段的槽，数组压缩为列表，OrderedMap 按源键顺序形成映射；空输入初始化所有目标为空集合。

结构化并发统一持有本组任务：首个失败或父取消时取消所有未完成任务，等待退出并丢弃全部槽。若采用 TaskGroup，需要显式拆开 ExceptionGroup 并保持 Panic 优先，不能让已包装的业务异常失去原选择器。并发原语必须由真实 Temporal 沙箱验证，避免使用已知存在确定性风险的无序 asyncio API。

Activity 使用 WAIT_CANCELLATION_COMPLETED 和心跳传递 Server 取消。包装器为业务函数构造进程内 AbortSignal，在取消及本地单次期限到达时置位并取消业务任务；同步阻塞函数不属于该异步适配器的支持范围。后台心跳和期限任务均在 finally 中取消并等待结束；即便业务函数吞取消后返回，也检查 attempt 已结束状态并丢弃迟到结果或错误。AbortSignal 是 Worker 内资源，不能作为 Python 对象序列化进 History。

重试只回滚解释器变量，不承诺回滚已发生的外部副作用。执行器负责按其业务语义使用幂等键、事务和取消资源清理。

## 历史压力与 Child Workflow

普通 workflow 调用使用当前 Workflow 内的函数帧；仅在到达调用边界且 Temporal 建议继续新历史时，把该次调用交给同一个解释器类型的 Child Workflow。子请求使用相同 bundle、绑定后的输入、宿主选项和增加后的 call_depth，返回相同 JSON 响应。

子调用禁用 Temporal 隐式 Workflow 重试，由父节点状态机唯一控制 DSL 重试；父取消须传播并等待子执行结束。子图 max_steps 通过可恢复错误载荷保留，StackOverflow 和根级 Panic 保持不可恢复。派生 Child Workflow 只隔离被调用工作流的后续历史，不清空父 Workflow 已有 History，也不作为任意长根循环的自动压缩保证。

## 工程边界

新增 uv workspace 和 `python/axcutor_runtime` 包，Python 最低 3.11。运行时依赖 temporalio、现有 axcutor-core Python 包，以及 Decimal JSON 编解码库；依赖和锁文件由对应工具解析生成。运行模块按 bundle/codec、errors、workflow 帧与调度、Activity ABI 与异步函数注册、worker 组装划分，避免为了对齐 RFC 示意目录生成空模块。

运行时文档记录公开请求形状、错误与默认期限、执行器取消义务、数值和键序边界，以及 Worker 的原生扩展沙箱透传设置。仓库检查入口纳入 Rust 内核、真实 Python 扩展和 Temporal 运行时；不手工修改 wheel、WASM glue、依赖锁或其他生成物。
