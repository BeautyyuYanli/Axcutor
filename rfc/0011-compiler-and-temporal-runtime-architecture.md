# RFC-0011: Axcutor 编译器前后端与 Temporal 运行时参考实现架构（Compiler & Temporal Runtime Architecture）

* **状态**：Implemented
* **所属分类**：编译器架构、跨端静态分析服务与分布式持久化运行时（Compiler Architecture & Durable Runtime Implementation）
* **核心解决的问题**：
  1. 确立 **“零独立低级 IR、以原地消糖归一化 AST（`NormalizedAst`）作为前后端唯一分界契约”** 的编译器设计原则，避免为原本已是显式 CFG 的语言重复发明字节码或三地址码；
  2. 采用 **Rust 编写编译器前端（5-Pass 静态验证）与确定性算子/契约内核（`axcutor-core`）**，一端通过 `wasm-bindgen` 编译为 WebAssembly 直接在浏览器低代码画布中零延迟运行，另一端通过 `PyO3`（`maturin`）编译为 Python 原生扩展供后端直接调用，确保前后端校验与求值逻辑 100% 同源；
  3. 采用 **Python（`temporalio` SDK）实现零独立数据库依赖的 Temporal 确定性图解释器（Compiler Backend）与主力 `run` 业务执行器**，完整定义 `NormalizedAst` 向 Temporal Workflow / Activity 模型的接入方式、四类可恢复异常与三类引擎 Panic 的映射机制、`retry` 下推与 Workflow 循环混合策略、`catch` 与 `asyncio.TaskGroup` 协同取消协议，以及通过 `@workflow.query` 暴露实时状态查询的完整闭环。
* **关联规范模块（`manual/spec/`）**：
  * [`index.md`](../manual/spec/index.md)
  * [`01-grammar-and-ast/`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md)
  * [`02-type-system-and-symbols/`](../manual/spec/02-type-system-and-symbols/01-typespec-well-formedness.md)
  * [`03-static-flow-analysis/`](../manual/spec/03-static-flow-analysis/01-cfg-topology-and-sese-validation.md)
  * [`04-runtime-execution-model/`](../manual/spec/04-runtime-execution-model/01-frame-memory-model.md)
  * [`05-executor-abi/01-run-executor-contract.md`](../manual/spec/05-executor-abi/01-run-executor-contract.md)

---

## 一、 背景与架构设计目标

在 [`manual/spec/`](../manual/spec/index.md) 完成形式化规范定义后，工程落地需要同时满足两个核心场景：

1. **独立的静态检查与画布智能辅助能力（Static Validator & Canvas Intelligence）**：
   * 能够独立于运行时引擎提供服务，支持对接前端低代码画布，实时完成语法检查、类型兼容性判定、CFG 拓扑与必达定值分析，并向画布输出节点级可用变量集合（Autocomplete）、动态派生端口（Port Derivation）及精确定位到节点/连边/端口的结构化诊断信息。
2. **基于 Temporal 的持久化工作流运行时（Durable Execution on Temporal）**：
   * 将通过静态检查的 Axcutor 工作流跑在 Temporal 之上，利用 Temporal 的事件溯源重放（Event Sourcing Replay）实现长周期容错执行，同时支持多样化的 `run` 业务执行器（以 Python 生态为主，兼顾其他语言微服务与外部 RPC 扩展）。

---

## 二、 核心架构决策

### 1. 为什么采用“归一化 AST 解释器”而非“独立低级 IR”或“AOT 源码生成”

#### (a) 拒绝冗余的独立低级 IR（No Separate Low-Level IR）
* **语言形态已是显式扁平 CFG**：根据 [`01-grammar-and-ast`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md) 与 [`02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md)，Axcutor 不存在复杂的嵌套算术/逻辑表达式树，所有运算均已拆解为显式的 `nodes.<id>` 单步节点，所有控制流均已表示为显式的 `edges` 连边。
* **复杂性全部收敛于编译期**：子类型偏序（$T_1 <: T_2$）、必达定值不动点（$Def_{\text{in}}^*$）与并发写集正交性（$OutVars(W_i) \cap OutVars(W_j) = \emptyset$）均在静态检查期消解。
* **原地消糖归一化 AST（`NormalizedAst`）即最佳契约**：编译器前端在完成静态验证的同时，仅需对原始 AST 执行轻量级的**原地消糖与索引化（In-place Normalization）**，即可直接交给 Temporal Workflow 解释器执行，彻底省去维护第二套 IR 数据结构与序列化转换的工程负担：
  1. **展开 `branches` 契约简写**：将 `<branch>: "<desc>"` 统一归一化为 `{ description: "<desc>", output: dom(node.output) }`；
  2. **展开 `catch` 通配符**：将 `catch.<branch>: "*"` 直接替换为扣除显式选择器后的差集数组；
  3. **消歧 `with.input` 参数四态**：将参数值直接标记为 `ArgValue = VarPathRef(root_var, fields[]) | ConstLiteral(json_val)`；
  4. **补全 `TypedDict.required`**：将省略 `required` 的结构体属性列表归一化填充为 `dom(properties)`；
  5. **构建 `edges` 邻接路由表与 `kill_vars` 掩码**：将 `edges: List<String>` 转化为按源端点索引的 $O(1)$ 查找表 `Map<SourceEndpointKey, EdgeAst>`，并在 `.none` / `.empty` 分支边上直接附着编译期推导出的反定值抹除集 `kill_vars: List<Identifier>`。

#### (b) 在 Temporal 上采用“确定性 AST 解释器（Interpreter Workflow）”而非 AOT 代码生成
* **任意有向环路拓扑适配**：Axcutor 允许任意满足“存在退环出口”的有向环路（SCC），无法总是直接映射为宿主语言的原生 `while` / `if` 语句；即使做 AOT 代码生成，产出的代码依然是包裹在循环内的 `match current_node_id` 状态机，与通用解释器在 Temporal 内部的执行形态完全一致。
* **支持低代码画布动态发布**：通过实现单一通用的 Temporal Workflow 入口 `ExecuteAxcutorWorkflow(bundle: WorkflowBundle, input: dict)`，用户在画布上修改并保存工作流后，只要通过编译器前端生成自包含的 `WorkflowBundle`（含入口工作流及传递引用的 `use: workflow:<path>` 归一化 AST 字典，保障重放期间定义不可变），即可直接投递给已常驻的 Temporal Worker 立即运行，无需重新编译和部署后端 Worker 容器。

---

### 2. 为什么选择“Rust 核心（`wasm` + `PyO3`）+ Python Temporal 后端”双语言架构

为兼顾“浏览器端直接离线运行静态检查”与“后端以 Python 为主的业务执行生态”，系统在语言选型上采用 **Rust + Python** 组合，并淘汰在后端引入 TypeScript/Node.js 的方案：

1. **单一事实来源（Single Source of Truth）**：
   * 编译器前端（5-Pass 静态检查）、运行期 `TypeSpec` 实例校验（$c \models T$）以及 16 个 `builtin:*` 算子的纯函数求值全部用 **Rust** 编写在无 I/O 的 `axcutor-core` 中。
   * 面向前端：通过 `wasm-bindgen` 编译为 WebAssembly，直接在浏览器画布中以微秒级延迟执行静态检查与端口/变量推导。
   * 面向后端：通过 `PyO3` + `maturin` 编译为 Python 原生扩展包，供 Python API 服务与 Temporal Workflow 解释器直接调用，彻底杜绝前后端分别用不同语言重写校验器导致的语义漂移。
2. **Python 相比 TypeScript 在 Temporal 解释器层的天然优势**：
   * **`OrderedMap` 键序严格保持**：[`05-builtin-operational-semantics.md`](../manual/spec/04-runtime-execution-model/05-builtin-operational-semantics.md) 要求 `OrderedMap` 在 `for_each`、`map_set`、`map_delete` 中严格保持插入顺序；JavaScript/V8 引擎对 `"0"`, `"1"` 等非负整数样式的字符串键会强制重排到对象最前，违反规范，而 Python 3.7+ 原生 `dict` 与 Rust `IndexMap` 均严格保持任意键的插入顺序。
   * **`integer` 与 `number` 严格区分**：Python 原生区分 `int`（任意精度整数）与 `float`（双精度浮点），天然契合 [`01-typespec-well-formedness.md`](../manual/spec/02-type-system-and-symbols/01-typespec-well-formedness.md) 与 `builtin:math` 的数值类型区分；而 JS/TS 只有统一的 `number`，JSON 反序列化后无法区分 `1` 与 `1.0`。
   * **沙箱直通原生扩展**：Temporal TypeScript SDK 将 Workflow 封装在剥离了 Node 原生能力的 V8 Isolate 中，难以直接加载本地原生扩展；而 Temporal Python SDK 的 `SandboxedWorkflowRunner` 仅需配置 `SandboxRestrictions.default.with_passthrough_modules("axcutor_core")`，即可在确定性 Workflow 协程中零开销直接调用 Rust 的 `PyO3` 函数。

---

### 3. 解释器层零独立数据库依赖原则（Zero External Database for Interpreter）

在 Temporal 架构下，**Axcutor 解释器本身是 100% 无状态（Stateless Worker）的，不需要为工作流执行引入任何独立的数据库、Redis 或外部消息队列**：
1. **状态机与栈帧自动持久化**：将编译好的自包含 `WorkflowBundle` 与初始 `input` 提交给 Temporal Server 后，Workflow 协程内存中的栈帧字典 `frame.env`（$\rho$）、当前控制令牌游标 `cursor`、步数计数器 `steps` 以及 `for_each` 槽位缓冲 `slots` 全部由 Temporal 的 **Event Sourcing（事件溯源重放）** 自动保障持久化。一旦 Worker 进程崩溃或重启，Temporal 通过重放已完成的 Activity 返回值与 Timer 事件，结合确定性的 `axcutor_core.eval_builtin()` 内存计算，在毫秒级将所有栈帧精确恢复至崩溃前的状态。
2. **实时运行状态通过 `@workflow.query` 直接暴露**：外部若需在运行中途查看当前活跃栈帧的变量快照、当前执行到的节点 ID 或已消耗的 `steps`，无需边跑边写外部数据库，只需在 `AxcutorInterpreterWorkflow` 上声明 `@workflow.query` 方法，即可通过 Temporal Client 随时零副作用地读取实时内存状态。
3. **执行轨迹与最终输出内置可查**：每个 `run` 节点的 `input_snapshot`、返回的 `output`、重试次数、耗时以及整个工作流到达 `end` / `end.<branch>` 后的最终返回值，均天然持久化在 Temporal Server 的 Event History 中。

---

## 三、 编译器前端（`axcutor-core`）5-Pass 流水线与画布服务接口

编译器前端必须采用**非短路（Non-short-circuiting）错误收集模式**，单次扫描尽可能多地报告图中的全部静态错误。

### 1. 五阶段静态分析流水线（5-Pass Pipeline）

| 阶段 | 对应规范章节 | 核心处理逻辑与产出 |
| :--- | :--- | :--- |
| **Pass 1: Parse & Desugar** | [`01-grammar-and-ast`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md) | 校验 `RootNode` / `InnerNode` 三态互斥鉴别与修饰符约束（如 `ActionNode` 禁止捕获 `"max_steps"`）；用 EBNF 解析 `edges` 字符串；提取 `run` 中 `variables.<E>` 引用；原地消糖生成初版 `NormalizedAst`。 |
| **Pass 2: Scoping & Symbol Table** | [`02/01~03`](../manual/spec/02-type-system-and-symbols/01-typespec-well-formedness.md), [`03/02`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md) | 校验 `TypeSpec` 四类构造器闭合互斥公理；校验 $\text{HostIn}(G) \cap \text{dom}(G.\text{variables}) = \emptyset$ 及 `for_each` 注入名无冲突；单遍 $O(N)$ 扫描四类显式源构建图级符号表 $\Sigma_G$，对同名重复声明强制执行双向结构化等价校验 $\Sigma_G(v) \equiv T$，并对 `default` 初值校验 $L \models T$。 |
| **Pass 3: Modular Type & Builtin Check** | [`02/02~05`](../manual/spec/02-type-system-and-symbols/02-type-equivalence-and-compatibility.md) | 实现静态字段路径推导 $\text{TypeOf}(\Sigma, v_0.f_1\dots f_k)$（中间步强制 `required` 且非空，末级非必填属性自动 $\text{MakeNullable}$）；基于单向子类型偏序 $T_{\text{actual}} <: T_{\text{expected}}$ 独立校验各节点绑定、`for_each` 一阶同构提升 $\text{Lift}(T_{\text{src}}, U)$ 及 16 个 `builtin:*` 泛型签名；推导各节点静态出口分支集合与边级 `kill_vars`。 |
| **Pass 4: CFG Topology & SESE Check** | [`03/01`](../manual/spec/03-static-flow-analysis/01-cfg-topology-and-sese-validation.md), [`03/04`](../manual/spec/03-static-flow-analysis/04-concurrent-disjoint-write-analysis.md) | 校验 `start` 与各节点出边穷尽且唯一、终点匹配宿主 `branches`；校验 Fork-Join 并发组成员 SESE 独占拓扑、无外露分支及写集合两两正交 $OutVars(W_i) \cap OutVars(W_j) = \emptyset$；执行正向/反向 BFS 与 Tarjan 强连通分量（SCC）算法验证无死代码且所有环路均有退环终态路径。 |
| **Pass 5: Definite Assignment** | [`03/03`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md) | 使用 `BitVec` 表示幂集格 $(\mathcal{P}(\text{dom}(\Sigma_G)), \subseteq)$，迭代求解边级传递方程 $Def_{\text{out}}(e_b) = (Def_{\text{in}}(N) \setminus KillVars(N, b)) \cup OutVars(N, b)$ 与交集汇合方程的最大不动点 $Def_{\text{in}}^*(X)$，校验节点读取断言 $Use(N) \subseteq Def_{\text{in}}^*(N)$ 与正常终点定值断言。 |

### 2. 面向低代码画布的扩展智能接口（Canvas Intelligence API）

除返回 `NormalizedAst` 外，编译器前端通过 WASM / Python 暴露以下副产物供画布直接消费：
* **图路径寻址诊断（Graph-Addressed Diagnostics）**：每条诊断包含精确的画布定位锚点 `{ scope_graph_path, target_kind: "node" | "edge" | "port" | "variable", node_id, branch_id, edge_index, param_path, code, message }`。
* **节点上下文可用变量与子类型过滤（Context-Aware Autocomplete）**：给定图内节点 $N$（及可选的目标参数期望类型 $T_{\text{expected}}$），基于 $Def_{\text{in}}^*(N)$ 与 $\Sigma_G$（含 `for_each` 局部注入表 $\Sigma_{\text{iter}}(N)$）枚举所有在此处**必定已定值**且满足 $\text{TypeOf}(\Sigma, E) <: T_{\text{expected}}$ 的顶层变量与合法 `.field` 投影路径。
* **节点动态端口推导（Dynamic Port Derivation）**：给定节点的 AST 配置与当前符号表，实时计算其暴露的规范化正常分支端口（如 `builtin:switch` 的枚举值分支、省略 `with.output` 时 `compare`/`logic` 的 `.true`/`.false`）与 `catch` 异常分支端口。

---

## 四、 编译器后端：标准化 AST 接入 Temporal 的完整执行机制

Temporal 解释器（`axcutor_runtime`）在确定性 Workflow 协程中接收自包含的 `WorkflowBundle = { "entry": Path, "modules": Map<Path, NormalizedRootNode> }`（将入口工作流及传递引用的子工作流归一化 AST 一次性打包传入，避免运行期文件 I/O 并防止重放时外部文件被修改导致非确定性错误）。

### 1. 节点模型向 Temporal Workflow / Activity 的映射边界

遵循 Temporal **“唯有外部非确定性 I/O 才进入 Activity”** 的准则，Axcutor 的三类节点按如下边界接入：

| 节点类型 | Temporal 执行载体 | 详细接入方式 |
| :--- | :--- | :--- |
| **`use: builtin:<op>`（16 个内建算子）** | **Workflow 协程内同步调用 Rust `PyO3`** | **绝不发起 Activity 或 Local Activity**。直接在 Workflow 内存中调用 `axcutor_core.eval_builtin(op, with_in, with_out, frame.env)`，微秒级返回 `(branch, delta, kill_vars)`，**零网络开销、零 Event History 事件占用**。 |
| **`ActionNode`（`run` 业务节点）** | **Temporal Activity（`workflow.execute_activity`）** | 唯一产生外部 I/O 的单元。解释器从 `frame.env` 提取 `node.input` 的深拷贝只读快照 `input_snapshot`，连同 `run_spec`、`output_schema`、`branches_contract` 封装为标准 ABI 请求投递至对应的 Activity `task_queue`。 |
| **`InlineGraphNode`（内联子图）** | **Workflow 内部子协程调用 + 内存 `BlockFrame`** | 在当前 Workflow 协程内新建独立的 `BlockFrame` 对象（`call_depth + 1`），递归调用单图控制令牌主循环 `_exec_graph()`。 |
| **`CallNode`（`use: workflow:<path>`）** | **Workflow 内部 `FunctionFrame` 或 `execute_child_workflow`** | 默认在当前 Workflow 内从 `bundle["modules"][path]` 取出目标 AST 并压入内存 `FunctionFrame`（`call_depth + 1`，零跨 Workflow 调度延迟）；当 `workflow.info().is_continue_as_new_suggested()` 为真（单工作流历史事件逼近 Temporal 50,000 条阈值）时，透明切换为派生 **Temporal Child Workflow**（携带当前 `call_depth + 1`）。 |

---

### 2. 单图控制令牌主循环（Graph Token Loop）

每张图的执行在 `_exec_graph()` 中表現为一个由 `NormalizedAst.edge_map` 驱动的确定性 `while` 循环：
1. **查表路由**：以当前游标 `cursor`（初始为 `"start"`，后续为 `"<node_id>"` 或 `"<node_id>.<branch_id>"`）在 `edge_map` 中 $O(1)$ 取出唯一出边 `edge`。
2. **边级反定值抹除（`kill_vars`）**：若 `edge` 包含编译期推导出的 `kill_vars`（如 `unwrap.none`、`array_get.none`、`map_get.none`、`math.none` 或 `array_shift.empty`），立即从 `frame.env` 中移除对应键（置为 `Unbound`）。
3. **目标端点转移**：
   * 若目标为 `end` / `end.<branch>`：从 `frame.env` 中提取该出口分支承诺产出的 `output` 字段子集，结束当前图循环并返回 `(branch, output_dict)` 原子提交至父栈帧；
   * 若目标为 `fail`：立即构造并抛出 `AxcutorUncaughtError(selector="execution")`，直接丢弃当前 `frame` 触发栈展开回滚；
   * 若目标为普通节点 `node_id`：调用节点执行状态机获得 `(chosen_branch, delta)`，执行 `frame.commit(delta)`，并将 `cursor` 更新为 `f"{node_id}.{chosen_branch}" if chosen_branch else node_id`；
   * 若该边为 `ForkJoinEdge(workers, join_target)`：进入并发屏障调度器执行 `workers`，全部正常完成后合并各分支暂存槽 $\Delta_i$，并将下一跳推进至 `join_target`。

---

### 3. 四类异常、原地重试（`retry`）与异常捕获（`catch`）接入 Temporal 的机制

规范在 [`04-exception-handling-and-unwinding.md`](../manual/spec/04-runtime-execution-model/04-exception-handling-and-unwinding.md) 中定义了 4 类可恢复异常选择器（`execution`、`timeout`、`contract`、`max_steps`）与 3 类不可拦截的引擎 Panic（`StackOverflowError`、`MaxStepsExceededError`、`CancelledError`）。它们接入 Temporal 的具体机制如下：

#### (a) 四类可恢复异常在 Temporal 中的产生与标准化标记
1. **`execution`**：
   * 当 `run` 节点的业务执行器发生故障时，Activity 包装器（`abi_contract.py`）将其捕获并抛出 Temporal 的 **`ApplicationError(message, type="execution")`**；
   * 当子图主动走向 `-> fail` 边时，Workflow 协程抛出 `AxcutorUncaughtError(selector="execution")`。
2. **`timeout`**：
   * 对 `ActionNode`（`run`）：直接将 `node.timeout_ms` 映射为 Temporal Activity 的原生配置 **`start_to_close_timeout=timedelta(milliseconds=node.timeout_ms)`**；超时后 Temporal 自动终止该次尝试并向 Workflow 抛出 `ActivityError(cause=TimeoutError(timeout_type=START_TO_CLOSE))`，Workflow 将其识别为 `selector = "timeout"`；
   * 对声明了 `timeout_ms` 的复合节点（`InlineGraphNode` / `CallNode`）：在 Workflow 层使用受 Temporal 虚拟时钟驱动的 `asyncio.wait_for(exec_subgraph(), timeout=node.timeout_ms / 1000.0)` 包裹单次尝试，超时自动取消子图内所有在途任务并映射为 `selector = "timeout"`。
3. **`contract`（前移至 Activity 边界校验以赋能原生重试）**：
   * 根据 [`01-run-executor-contract.md`](../manual/spec/05-executor-abi/01-run-executor-contract.md)，当业务执行器返回 `{ branch, output }` 后，**Activity 包装器在返回给 Temporal Server 之前**（以及 Workflow 收到返回值后作为防御性兜底），立即调用 `axcutor_core.validate_run_contract(raw_resp, output_schema, branches_contract)` 校验分支合法性与深层 `TypeSpec` Schema（含 `TypedDict` 闭合性检查）并过滤顶层多余字段；
   * 一旦校验违约，Activity 包装器当场抛出 **`ApplicationError(message, type="contract")`**。将 `contract` 校验前移至 Activity 末尾的关键收益是：**当用户配置了 `retry.on: ["contract"]`（例如大模型结构化输出格式错误时重试）时，可以直接利用 Temporal 原生的 Activity 重试机制在后台完成重试，无需将错误 payload 反复写入 Workflow Event History**。
4. **`max_steps`**：
   * 仅由子图（`InlineGraphNode` 或 `CallNode`）在 `frame.steps > graph.max_steps` 时触发：子图立即取消其内部所有在途任务、销毁自身栈帧，并向父节点边界抛出 `AxcutorUncaughtError(selector="max_steps")`（若发生在最顶层根图，则直接抛出不可恢复的 `MaxStepsExceededError` 终止整个 Workflow）。

#### (b) `retry` 接入策略：Temporal 原生 `RetryPolicy` 下推与 Workflow 循环混合模式
* **对 `ActionNode`（`run` 节点）——优先下推至 Temporal 原生 `RetryPolicy`**：
  * 当节点未配置 `retry`，或配置了 `retry` 且 `"timeout" in node.retry.on`（默认 `retry.on` 为 `["execution", "timeout"]`，绝大多数场景均满足）时，直接将重试策略下推给 Temporal `execute_activity` 的原生 `RetryPolicy`（从而使多次重试不膨胀 Workflow 历史事件）：
    * `maximum_attempts = node.retry.max_attempts`（未配 `retry` 时为 `1`）；
    * `initial_interval = timedelta(milliseconds=node.retry.backoff_ms)`；
    * `backoff_coefficient = 1.0`（严格对齐规范定义的固定间隔 `backoff_ms` 退避，禁用指数倍增）；
    * `non_retryable_error_types = list({"execution", "contract"} - set(node.retry.on))`（取补集：不在 `retry.on` 中的异常类型标记为不可重试，一旦发生立即中断 Activity 重试并抛回 Workflow 进入 `catch`）。
  * **特例回退（当 `node.retry.on` 显式排除了 `"timeout"` 时）**：由于 Temporal 原生 `RetryPolicy` 无法通过 `non_retryable_error_types` 禁止对 `START_TO_CLOSE` 超时的重试，若用户显式配置了如 `retry: { max_attempts: 3, on: ["execution"] }`（即超时不重试），解释器自动将该 Activity 的底层 `maximum_attempts` 设为 `1`，改由下方的 Workflow 层重试循环接管。
* **对复合节点（`InlineGraphNode` / `CallNode`）——由 Workflow 层快照重试循环接管**：
  * 复合节点的单次尝试跨越多个内部步骤与局部状态变更，解释器在 Workflow 协程内用 `for attempt in range(1, max_attempts + 1):` 循环包裹：
  * 每次尝试均从父帧锁定的同一份只读快照 `S_in` 构造全新的子栈帧 `Frame`；若子图抛出可恢复异常 `E`，立即丢弃该脏子栈帧（完成 All-or-Nothing 回滚）；若 `E.selector in node.retry.on` 且 `attempt < max_attempts`，则调用 **`await workflow.sleep(node.retry.backoff_ms / 1000.0)`**（映射为 Temporal 持久化 Timer）等待退避后进入下一次尝试。

#### (c) `catch` 路由与未捕获异常栈展开（Unwinding）
* 当节点级重试耗尽或异常不属于 `retry.on` 而逃逸至节点边界时，由 Workflow 协程的 `try ... except` 块统一裁决：
  1. **不可恢复 Panic 直通**：若异常为 `StackOverflowError`、`MaxStepsExceededError` 或 `asyncio.CancelledError`，严禁拦截，直接向上 `raise`。
  2. **命中 `node.catch.<c_branch>`**：若异常选择器 `selector` 属于某条已展开的 `node.catch[c_branch]` 列表，则吞掉该异常，**不向当前图栈帧提交任何输出（`delta = {}`）**，返回异常分支名 `c_branch`，驱动外层单图主循环沿 `N.<c_branch> -> target` 边继续前进。
  3. **未命中任何 `catch`**：将 `AxcutorUncaughtError(selector)` 向所属图栈帧外层 `raise`，触发下述结构化并发 Fail-Fast 取消与父栈帧回滚。

---

### 4. Fork-Join、`for_each` 并发屏障与 `abort_signal` 协同取消协议

Temporal Python SDK 的确定性事件循环原生支持 Python 3.11+ 的 `asyncio.TaskGroup` 与 `asyncio.Semaphore`，可一对一实现 [`02-fork-join-and-for-each-scheduler.md`](../manual/spec/04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md)：

1. **Fork-Join 异构并发组（`src -> [W_1, ..., W_k] -> join_target`）**：
   * 锁定父帧快照 $\rho_{\text{fork}}$，分配 $k$ 个独立输出槽 `deltas = [{} for _ in range(k)]`，在 `async with asyncio.TaskGroup() as tg:` 中并发启动 $k$ 个分支协程；
   * 全部正常完成退出 `TaskGroup` 屏障后，按声明顺序 $i = 0 \dots k-1$ 依次将 `deltas[i]` 合并回 `frame.env`。
2. **`for_each` 同构集合遍历与保序汇聚**：
   * 求值源集合得到长度为 $n$ 的序列 `[(k_0, v_0), ..., (k_{n-1}, v_{n-1})]`（当 $n = 0$ 时不计步，直接将所有 `OutVars(M)` 初始化为 `[]` 或 `{}` 并沿默认边前进）；
   * 当 $n \ge 1$ 时，分配定长槽数组 `slots = [None] * n` 与信号量 `sem = asyncio.Semaphore(for_each.concurrency)`；在 `asyncio.TaskGroup` 中派生 $n$ 个元素协程，每个协程在 `async with sem:` 内获取配额后先执行 `frame.increment_step()`，再运行单次迭代的节点状态机（含独立的单元素 `retry` 与 `catch`），正常完成写 `slots[i] = out_i`，被单元素 `catch` 拦截则写 `slots[i] = {}`；
   * 屏障收敛后，严格按源集合下标 $i = 0 \dots n-1$ 扫描 `slots[i]` 构造目标 `list` 或保序 `dict`，一次性原子写回外层 `frame.env`。
3. **Fail-Fast 协同取消与 `abort_signal` 端到端传递**：
   * 若 `TaskGroup` 内任一并发分支或元素迭代抛出未被局部 `catch` 拦截的异常（或任何协程触发 `frame.steps > frame.max_steps`），`asyncio.TaskGroup` **立即自动向组内所有尚在运行的兄弟协程广播不可捕获的 `asyncio.CancelledError`**；
   * 对于正在等待 `workflow.execute_activity` 的在途分支，由于配置了 `cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED`，Temporal 会立即将取消请求下发给对应的 Activity Worker；
   * Activity 包装器（`abi_contract.py`）通过后台心跳（Heartbeat）监听到取消信号后，立即置位业务执行器的 **`abort_signal`**（取消底层 HTTP 请求、数据库游标或沙箱子进程），且解释器侧遵循 [`01-run-executor-contract.md`](../manual/spec/05-executor-abi/01-run-executor-contract.md) 第四节静默丢弃已超时或已取消尝试的一切迟到结果；待在途任务全部退出后，丢弃全部暂存槽 `deltas` / `slots`，保持外层 `frame.env` 分毫无损并向上冒泡异常。

---

### 5. 核心接入代码骨架（Reference Implementation Skeleton）

```python
@workflow.defn
class AxcutorInterpreterWorkflow:
    def __init__(self) -> None:
        self._root_frame: Frame | None = None

    @workflow.query
    def get_runtime_snapshot(self) -> dict:
        """零外部数据库依赖：供外部随时通过 Temporal Query 实时读取当前栈帧状态与步数"""
        return self._root_frame.to_diagnostic_dict() if self._root_frame else {}

    @workflow.run
    async def run(self, bundle: dict, root_input: dict) -> dict:
        root_ast = bundle["modules"][bundle["entry"]]
        branch, output = await self._exec_root_or_subgraph(bundle, root_ast, root_input, call_depth=1)
        return {"branch": branch, "output": output}

    async def _exec_action_node(self, node: dict, input_snapshot: dict) -> tuple[str | None, dict]:
        retry_cfg = node.get("retry")
        # 当未配置 retry，或 retry.on 包含 "timeout" 时，直接下推至 Temporal 原生 Activity RetryPolicy
        can_use_native_retry = (retry_cfg is None) or ("timeout" in retry_cfg["on"])

        try:
            if can_use_native_retry:
                non_retryable = (
                    list({"execution", "contract"} - set(retry_cfg["on"]))
                    if retry_cfg else ["execution", "contract"]
                )
                raw_resp = await workflow.execute_activity(
                    "execute_run",
                    {
                        "run_spec": node["run"],
                        "input_snapshot": input_snapshot,
                        "output_schema": node["output"],
                        "branches_contract": node["branches"],
                    },
                    task_queue=route_task_queue(node["run"]),
                    start_to_close_timeout=(
                        timedelta(milliseconds=node["timeout_ms"]) if node.get("timeout_ms") else None
                    ),
                    retry_policy=RetryPolicy(
                        maximum_attempts=retry_cfg["max_attempts"] if retry_cfg else 1,
                        initial_interval=(
                            timedelta(milliseconds=retry_cfg.get("backoff_ms", 0))
                            if retry_cfg else timedelta(seconds=1)
                        ),
                        backoff_coefficient=1.0,
                        non_retryable_error_types=non_retryable,
                    ),
                    cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
                delta = axcutor_core.validate_run_contract(raw_resp, node["output"], node["branches"])
                return raw_resp.get("branch"), delta
            else:
                # 当 retry.on 显式排除了 "timeout" 时，使用 Workflow 层重试循环
                return await self._exec_action_with_workflow_retry(node, input_snapshot)

        except ActivityError as err:
            selector = classify_activity_error(err)  # 还原为 "execution" | "timeout" | "contract"
            for catch_branch, selectors in node.get("catch", {}).items():
                if selector in selectors:
                    return catch_branch, {}  # 命中 catch：丢弃局部产出，转入异常分支出边
            raise AxcutorUncaughtError(selector, str(err)) from err
```

---

## 五、 仓库工程目录结构规范

代码库采用 **Rust Cargo Workspace + Python `uv`（`maturin`）Workspace** 的混合 Monorepo 结构，各模块职责与 [`manual/spec/`](../manual/spec/index.md) 章节严格对齐：

```txt
/
├── Cargo.toml                             # Rust Workspace 根配置 (members = ["crates/*"])
├── pyproject.toml                         # uv Python Workspace 根配置 (管理 axcutor-py 与 axcutor-runtime)
├── manual/                                # 语言形式化规范 (manual/spec) 与使用手册 (manual/docs)
├── rfc/                                   # 架构与语言设计演进提案
│
├── crates/                                # 【第一层：Rust 编译器前端 + 运行时确定性纯函数内核】
│   ├── axcutor-core/                      # 纯 Rust 核心 crate（零 I/O、零系统调用，唯一事实来源）
│   │   ├── Cargo.toml                     # 依赖: serde, serde_json(preserve_order), serde_yaml, indexmap, bitvec, regex, thiserror
│   │   └── src/
│   │       ├── lib.rs
│   │       ├── ast/                       # [spec/01-grammar-and-ast] 归一化 AST 模型
│   │       │   ├── mod.rs
│   │       │   ├── document.rs            # NormalizedRootNode, InnerNode(Action/InlineGraph/Call), Modifiers
│   │       │   ├── edge.rs                # DirectEdge, ForkJoinEdge, SourceEndpointKey, 邻接路由表
│   │       │   ├── binding.rs             # FieldPathExpr, ArgValue(VarPathRef | ConstLiteral), RunVarRef 提取
│   │       │   └── typespec.rs            # TypeSpec 四类闭合构造器 (Scalar/Array/TypedDict/OrderedMap) 及辅助变换
│   │       │
│   │       ├── compiler/                  # [spec/01 ~ 03] 编译器前端 5-Pass 非短路静态检查流水线
│   │       │   ├── mod.rs                 # compile_workflow(raw_doc, sig_provider) -> CompileOutput
│   │       │   ├── diagnostics.rs         # 结构化报错 (含错误码、YAML span、画布 scope_path/node_id/edge_idx/port)
│   │       │   ├── pass1_desugar.rs       # 语法良构检查 + 原地消糖 (展开 branches 简写、catch "*"、补全 required)
│   │       │   ├── pass2_symbols.rs       # [spec/02/01~03 & 03/02] 作用域无冲突、单遍 O(N) 构建 Sigma_G、类型等价 ===
│   │       │   ├── pass3_typecheck.rs     # [spec/02/02~05] 子类型 T1 <: T2、字段投影 TypeOf、16 个 builtin 泛型推导与 KillVars 标记
│   │       │   ├── pass4_topology.rs      # [spec/03/01 & 03/04] 出边穷尽唯一、Fork-Join SESE 独占、并发写集互斥、BFS + Tarjan SCC
│   │       │   └── pass5_dataflow.rs      # [spec/03/03] BitVec 幂集格最大不动点求解 Definite Assignment (Def_in / Def_out)
│   │       │
│   │       └── kernel/                    # [spec/04 & 05] 供运行时解释器直接调用的无状态纯函数内核
│   │           ├── mod.rs
│   │           ├── conformance.rs         # 实例兼容性校验 (c |= T) 与 run 节点返回载荷边界校验 (过滤额外字段/抛 contract 异常)
│   │           ├── eval_arg.rs            # Eval(rho, arg) 字段路径投影求值 (缺失非必填叶子属性转 null)
│   │           └── builtins/              # [spec/04/05] 16 个 builtin:* 纯内存写时复制求值器
│   │               ├── mod.rs             # eval_builtin(op, with_in, with_out, env) -> (branch, delta, kill_vars)
│   │               ├── scalar.rs          # assign, unwrap
│   │               ├── control_math.rs    # switch, compare (深度值相等), logic, math (含除零分支), length (Unicode 码点)
│   │               ├── array.rs           # array_get (负索引), array_shift, array_append, array_concat, array_flatten
│   │               └── collection.rs      # map_get, map_set (IndexMap 保序), map_delete, object_merge
│   │
│   ├── axcutor-wasm/                      # 面向浏览器低代码画布的 WASM 绑定 crate (wasm-bindgen)
│   │   ├── Cargo.toml
│   │   └── src/
│   │       └── lib.rs                     # 导出给前端 JS/TS: validate_graph(), get_available_vars_at_node(), derive_node_ports()
│   │
│   └── axcutor-py/                        # 面向 Python 后端的 PyO3 绑定 crate (由 maturin 构建为 axcutor_core Python 包)
│       ├── Cargo.toml
│       ├── pyproject.toml                 # build-system = "maturin"
│       └── src/
│           └── lib.rs                     # 导出给 Python: compile_workflow(), extract_signature(), eval_builtin(), validate_run_contract()
│
└── python/                                # 【第二层：Python Temporal 编译器后端与执行器生态】
    └── axcutor_runtime/
        ├── pyproject.toml                 # 依赖: temporalio, axcutor-core (workspace)
        ├── src/
        │   └── axcutor_runtime/
        │       ├── __init__.py
        │       ├── bundle.py              # WorkflowBundle 组装器：调用 axcutor_core 编译入口及传递引用的 use: workflow:<path>
        │       ├── errors.py              # [spec/04/04] 四类可恢复异常选择器与三类不可捕获 Panic (StackOverflow/MaxSteps/Cancelled)
        │       │
        │       ├── workflow/              # 【Temporal Workflow 解释器：跑在 Deterministic Sandbox 内】
        │       │   ├── __init__.py
        │       │   ├── interpreter.py     # @workflow.defn 主入口与单图控制令牌循环 (维护 steps 护栏与邻接表跳转)
        │       │   ├── frame.py           # [spec/04/01] FunctionFrame / BlockFrame / IterationFrame 内存模型与 call_depth(<=64)
        │       │   ├── node_runner.py     # [spec/04/04] 节点执行状态机：快照锁定 -> Activity/子图/builtin 分派 -> 契约校验 -> retry/catch
        │       │   ├── scheduler.py       # [spec/04/02] Fork-Join (asyncio.TaskGroup) 与 for_each (Semaphore + Slots 保序汇聚 + Fail-Fast 取消)
        │       │   └── router.py          # 根据 run_spec 将不同种类的 run 节点路由至对应的 Temporal Activity Task Queue
        │       │
        │       ├── executors/             # 【Temporal Activity Workers：跑业务 run 节点，脱离沙箱】
        │       │   ├── __init__.py
        │       │   ├── abi_contract.py    # [spec/05/01] 标准 Activity 包装器：处理只读快照深拷贝、心跳监听转 abort_signal、异常封装为 ExecutionError
        │       │   ├── python_runner.py   # 主力 Python 执行器实现 (LLM 结构化输出、本地注册 Python 函数、脚本沙箱等)
        │       │   └── http_rpc_runner.py # 通用 HTTP / gRPC 远程执行器实现 (用于快速对接外部异构服务)
        │       │
        │       └── worker.py              # Worker 启动入口：配置 SandboxRestrictions.with_passthrough_modules("axcutor_core")
        │
        └── tests/
            ├── test_spec_conformance.py   # 自动扫描 manual/docs/ 下所有示例 YAML 进行编译与 Temporal Time-Skipping 端到端测试
            └── test_fault_injection.py    # 针对 retry/catch/fail、timeout_ms、max_steps 与并发 Fail-Fast 回滚的边界测试
```

---

## 六、 分阶段实施路线图（Implementation Milestones）

1. **阶段 1：`crates/axcutor-core` 静态检查器与纯算子内核**：
   * 完成 `ast`、`compiler`（Pass 1 ~ Pass 5）与 `kernel`（`conformance` + 16 个 `builtins`），并编写自动化测试扫描 `manual/docs/guide/` 与 `manual/docs/cookbook/` 中的全部 YAML 代码块确保 100% 编译通过。
2. **阶段 2：`crates/axcutor-py` 与 `crates/axcutor-wasm` 双端绑定**：
   * 使用 `PyO3` + `pythonize` 封装 Python 绑定，使用 `wasm-bindgen` 封装浏览器 WASM 绑定，验证同一套用例在 Python 与无头浏览器中的输出完全一致。
3. **阶段 3：`python/axcutor_runtime` Temporal 解释器与 ABI 测试闭环**：
   * 实现基于 `temporalio` 的 `workflow/` 解释器与 `executors/abi_contract.py` 标准包装器，利用 Temporal 内置的 Time-Skipping 测试环境验证重试退避、超时取消、并发 Fail-Fast 回滚及 Cookbook 复杂工作流的端到端执行。
