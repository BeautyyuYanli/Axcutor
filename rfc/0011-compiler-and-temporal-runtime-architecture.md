# RFC-0011: Axcutor 编译器前后端与 Temporal 运行时参考实现架构（Compiler & Temporal Runtime Architecture）

* **状态**：Proposed
* **所属分类**：编译器架构、跨端静态分析服务与分布式持久化运行时（Compiler Architecture & Durable Runtime Implementation）
* **核心解决的问题**：
  1. 确立 **“零独立低级 IR、以原地消糖归一化 AST（`NormalizedAst`）作为前后端唯一分界契约”** 的编译器设计原则，避免为原本已是显式 CFG 的语言重复发明字节码或三地址码；
  2. 采用 **Rust 编写编译器前端（5-Pass 静态验证）与确定性算子/契约内核（`axcutor-core`）**，一端通过 `wasm-bindgen` 编译为 WebAssembly 直接在浏览器低代码画布中零延迟运行，另一端通过 `PyO3`（`maturin`）编译为 Python 原生扩展供后端直接调用，确保前后端校验与求值逻辑 100% 同源；
  3. 采用 **Python（`temporalio` SDK）实现 Temporal 确定性图解释器（Compiler Backend）与主力 `run` 业务执行器**，精确映射 Axcutor 的三类栈帧内存模型、16 个内建算子零 I/O 同步求值、Fork-Join / `for_each` 结构化并发与协同取消、`max_steps` / `max_call_depth` 护栏及多语言异构 Activity 路由。
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

## 四、 编译器后端（Temporal 运行时）映射契约

Temporal 解释器（`axcutor_runtime`）在确定性 Workflow 协程中接收自包含的 `WorkflowBundle`，按以下规则将 [`04-runtime-execution-model`](../manual/spec/04-runtime-execution-model/01-frame-memory-model.md) 与 [`05-executor-abi`](../manual/spec/05-executor-abi/01-run-executor-contract.md) 映射至 Temporal 原语：

| Axcutor 运行时概念 | Temporal 原语与实现机制 | 关键一致性保障 |
| :--- | :--- | :--- |
| **`builtin:*`（16 个内建算子）** | **Workflow 协程内直接同步调用 `axcutor_core.eval_builtin()`** | **零 Activity 开销、零 Event History 占用**；Rust 内核基于 `IndexMap` 与写时复制返回 `(branch, delta, kill_vars)`，微秒级完成状态转移且永不抛可恢复异常。 |
| **`ActionNode`（`run` 节点）** | **Temporal Activity（`MaximumAttempts=1`）+ Workflow 层状态机** | 禁用 Temporal 底层盲目重试，将 `StartToCloseTimeout` 设为 `node.timeout_ms`；Activity 返回后在 Workflow 层调用 `axcutor_core.validate_run_contract()` 检验 `branch` 与 `TypeSpec`（违约则生成 `contract` 异常，过滤额外顶层字段）。 |
| **`retry` / `catch` 状态机** | **Workflow 层循环 + `await workflow.sleep(backoff_ms / 1000)`** | 统一支持对 `["execution", "timeout", "contract", "max_steps"]` 四类选择器的精细匹配；不仅适用于 `ActionNode`，同样一致作用于 `InlineGraphNode` 与 `CallNode` 的快照回滚重试。 |
| **三类栈帧与 `call_depth` 护栏** | **Workflow 内存栈帧对象（`Frame`）+ 深度计数器（$d \le 64$）** | 环境 $\rho$ 以字典存储（键缺失表示 `Unbound`，值为 `None` 表示 JSON `null`）；进入子图提取只读深拷贝快照，到达 `fail` 或未捕获异常直接丢弃子帧对象，天然实现 All-or-Nothing 事务回滚；当单 Workflow 历史事件数逼近 Temporal 阈值时，支持透明切换为 Child Workflow / `ContinueAsNew`。 |
| **Fork-Join 与 `for_each` 并发屏障** | **`asyncio.TaskGroup` + `asyncio.Semaphore` + 定长 `Slots[0..n-1]`** | 并发分支读取锁定的只读快照 $\rho_{\text{fork}}$ 并写入独立槽位；`for_each` 严格按源集合序号 $0 \dots n-1$ 扫描 `Slots` 保序汇聚；任一分支抛出未捕获异常或 `steps > max_steps` 时，`TaskGroup` 立即向在途兄弟任务广播不可捕获的 `CancelledError` 并丢弃全部槽位。 |
| **`abort_signal` 与迟到结果丢弃** | **Activity Heartbeat 取消传递 + Workflow 协程状态守卫** | Activity 包装器通过心跳协程监听 Temporal 取消请求并触发业务执行器的 `abort_signal`；Workflow 侧一旦判定某次尝试已超时或被取消，立即关闭该次尝试的状态机通道，静默丢弃后续任何迟到返回或异常。 |
| **多语言异构 `run` 执行器扩展** | **基于 `run_spec` 的动态 Temporal `task_queue` 路由** | 主力 Python 执行器可直接与解释器同进程或分队列部署；其他语言（Go / Node.js / 容器沙箱）只需监听独立 `task_queue` 实现标准 JSON ABI 即可接入，契约校验由解释器侧的 Rust 内核统一兜底。 |

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
