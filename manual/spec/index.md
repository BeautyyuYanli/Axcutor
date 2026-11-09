# Axcutor 语言与运行时形式化规范（Specification）

本规范定义 Axcutor 工作流语言的形式化语义与实现契约（Single Source of Truth），旨在为静态检查器（Static Validator）、运行时调度器（Runtime Scheduler）以及业务执行器（Executor）提供无二义性的形式化判定标准。

---

## 1. 规范适用范围与实现一致性（Conformance）

任何声称符合 Axcutor 标准的系统组件，必须严格满足对应角色的一致性判定准则：

### 1.1 静态检查器一致性（Static Validator Conformance）
合规的静态检查器必须在工作流进入运行期前完成全量静态验证，且满足以下完备性与健全性要求：
- **拒绝一切非法程序**：对于任何违反词法/语法结构、类型良构性、先声明后使用原则、静态子类型兼容规则、控制流单入单出（SESE）拓扑约束、必达定值（Definite Assignment）不动点约束或并发写集无交集约束的工作流定义，必须在编译期精确拦截并拒绝生成可执行图表示。
- **零隐式转换与零静默忽略**：严禁在未声明变量、类型不匹配或悬空控制边上执行隐式类型强转、默认值填充或静默剪枝。
- **静态分析确定性**：给定相同的 AST 输入与跨工作流接口签名，符号表决议、子类型判定与数据流不动点求解必须产生唯一且可复现的判定结果。

### 1.2 运行时调度器一致性（Runtime Scheduler Conformance）
合规的运行时调度器必须严格按照操作语义执行已通过静态验证的控制流图（CFG）：
- **状态隔离与拷贝语义**：严格维护栈帧（`FunctionFrame`、`BlockFrame`、`IterationFrame`）的物理隔离内存环境 $\rho$，并在变量跨帧传递、并发分叉（Fork）与执行器入参构造时强制执行深拷贝快照隔离（Copy-in / Copy-out）。
- **调度确定性与资源配额执守**：在无外部非确定性 I/O 干扰的前提下，变量汇聚结果必须与并发分支的实际物理完成时序无关；严格执行步数预算（`max_steps`）、调用栈深度上限（`max_call_depth`）及单次尝试超时（`timeout_ms`）的硬性配额裁决。
- **结构化异常与协同取消语义**：严格遵循重试退避（`retry`）、异常捕获物化（`catch`）、帧回滚传播（`-> fail`）及并发组 Fail-Fast 协同取消的状态转移公理。

### 1.3 业务执行器一致性（Executor Conformance）
合规的业务执行器（包括宿主自定义处理器与内置 `builtin:*` 算子）必须遵守执行器二进制接口（ABI）契约：
- **只读入参契约**：将接收到的 `input_snapshot` 视为不可变快照，不得通过共享引用篡改调度器栈帧状态。
- **出口分支与输出契约**：完成计算后返回的出口分支标识符必须属于节点声明的规范化分支集合（`branches_contract`），且产出的 `output` 负载必须严格符合目标变量声明的静态类型契约（`output_schema`）；内部执行器违约由调度器在 ABI 边界拦截并升格为标准运行期异常。`signal` 的外部请求违约在 Update Validator 中拒绝，保持节点等待状态。

---

## 2. 核心安全性与确定性不变式（Core Soundness Invariants）

本规范的形式化公理体系共同保障已通过静态检查的 Axcutor 工作流在运行期恒满足以下四大核心不变式：

### 2.1 类型安全性（Type Soundness & Value Preservation）
若工作流图 $G$ 通过静态类型检查，则在运行期任意时刻、任意活跃栈帧的变量环境 $\rho$ 中，任何已绑定变量 $v$ 的运行时值必为其静态声明类型 $\Sigma_G(v)$ 的合法实例：
$$\forall v \in \text{dom}(\Sigma_G).\ \rho(v) \neq \text{Unbound} \implies \rho(v) \models \Sigma_G(v)$$
由于容器类型在运行期具有不可变（Immutable）语义且变量赋值仅在声明的子类型偏序（$T_{\text{src}} <: T_{\text{dst}}$）下发生，运行期绝不会出现由容器协变导致的堆污染或字段路径投影越界。

### 2.2 全路径必达定值安全性（Definite Assignment Soundness）
若工作流图 $G$ 通过静态必达定值数据流分析，则在运行期沿任意合法控制流路径到达节点 $X$ 并准备求值其输入引用集 $InVars(X)$ 时，所有被引用变量在当前环境 $\rho$ 中必定已处于定值绑定状态：
$$\forall X \in \text{Nodes}(G),\ \forall v \in InVars(X).\ v \in Def_{\text{in}}^*(X) \implies \rho_X(v) \neq \text{Unbound}$$
除非节点显式激活了含 `default` 兜底策略的容错访问规则，否则运行期绝不会发生读取未初始化变量（Read-of-Unbound-Variable）的未定义行为。

### 2.3 并发无数据竞争确定性（Data-Race Freedom & Deterministic Aggregation）
对于任意并发分叉节点（静态并发组或动态 `for_each` 迭代组），各并发执行分支在运行期均持有父帧环境 $\rho$ 的只读快照副本，并在独立的子帧或槽位缓冲区中执行写操作：
- **静态并发组写集正交**：任意两个并发子窗口 $W_i, W_j\ (i \neq j)$ 的可能写入变量集满足 $OutVars(W_i) \cap OutVars(W_j) = \emptyset$，在 Join 屏障处通过无交集映射合并，提交结果与各分支物理完成先后顺序严格无关。
- **动态迭代保序汇聚**：`for_each` 的 $N$ 个并发或串行迭代实例严格按源集合的确定性遍历次序 $i \in [0, N-1]$ 将单步产出写入独立槽位 $\text{Slot}[i]$，汇聚结果唯一取决于源集合内容与各槽位局部计算结果。

### 2.4 栈帧事务原子性（Transactional Frame Atomicity）
任何复合结构节点（子图块 `graph`、跨工作流调用 `use` 及并发/迭代组）的执行均具备 All-or-Nothing 事务语义：
- 仅当复合结构沿正常出口（`-> next` 或合法具名分支）完结时，其内部产出的输出变量绑定才原子性地提交（Commit）至父栈帧环境；
- 若复合结构因未捕获异常、契约违约、超时或显式 `-> fail` 终止，子栈帧及其全部中间状态立即销毁回滚（Rollback），父栈帧原有的变量环境 $\rho_{\text{parent}}$ 保持分毫无损（若被父节点 `catch` 捕获，则仅原子写入声明的异常载荷变量）。

---

## 3. 全局形式化记号约定（Formal Notation Conventions）

为保证整部规范中公理、推理规则与状态转移方程的严谨统一，全局采用以下数学记号约定：

| 记号 | 名称 | 形式化定义与语义说明 |
| :--- | :--- | :--- |
| $\Sigma_G$ | 静态符号表（Static Symbol Table） | 偏函数 $\text{VarName} \rightharpoonup \text{TypeSpec}$，将图 $G$ 作用域内的合法变量名映射为其唯一静态类型定义。 |
| $\Sigma_{\text{iter}}(M)$ | 迭代局部符号表（Iteration Local Symbol Table） | `for_each` 节点 $M$ 注入单次迭代作用域的隐式只读绑定表，包含元素变量 $v_{\text{item}}$ 与键/索引变量 $v_{\text{idx}}$ 的静态类型。 |
| $\rho$ | 运行期变量环境（Runtime Environment） | 映射 $\text{VarName} \to \text{Value} \cup \{\text{Unbound}\}$，表示特定栈帧在运行期某一时刻的变量内存快照。 |
| $\text{Unbound}$ | 未绑定哨兵（Unbound Sentinel） | 标识变量已在符号表 $\Sigma_G$ 中声明但尚未在当前运行期环境 $\rho$ 中获得有效赋值的特殊状态（注意：$\text{Unbound} \neq \text{null}$）。 |
| $T_1 \equiv T_2$ | 静态类型等价（Type Equivalence） | 满足自反、对称与传递性的结构化类型完全等价关系；同帧内对同一变量的重复类型声明必须满足 $T_1 \equiv T_2$。 |
| $T_1 <: T_2$ | 静态子类型偏序（Subtyping Relation） | 表示类型 $T_1$ 是类型 $T_2$ 的安全子类型（即任何 $T_1$ 的合法实例均可安全赋值给声明为 $T_2$ 的目标位置，无需运行期转换）。 |
| $c \models T$ | 字面量/值实例兼容（Instance Conformance） | 判定编译期静态字面量或运行期具体数据值 $c$ 符合类型规格 $T$ 的结构与值域约束。 |
| $\text{Lift}(T_{\text{src}}, U)$ | 一阶同构集合类型提升（Collection Type Lifting） | 根据 `for_each` 源集合类型 $T_{\text{src}}$ 的容器构造子与单步产出类型 $U$，构造同构目标集合类型的类型算子（如 `list<X>` 提升为 `list<U>`，`map<K, V>` 提升为 `map<K, U>`）。 |
| $InVars(X)$ / $OutVars(X)$ | 节点读/写变量集（Read / Write Variable Sets） | 分别表示节点 $X$ 在执行前需读取的源变量集合，以及执行后可能写入父作用域的目标变量集合。 |
| $Def_{\text{in}}(X)$ / $Def_{\text{out}}(X)$ | 必达定值集（Definite Assignment Sets） | 数据流分析中，沿所有可能控制流路径到达节点 $X$ 入口前（$Def_{\text{in}}$）与经特定出边离开节点 $X$ 后（$Def_{\text{out}}$）必定已赋值的变量集合。 |
| $A \uplus B$ | 无交集并（Disjoint Union） | 仅当集合或映射定义域满足 $A \cap B = \emptyset$（或 $\text{dom}(A) \cap \text{dom}(B) = \emptyset$）时有定义的并集运算，用于静态符号注入与并发无冲突状态合并。 |
| $\rho_1 \triangleleft \rho_2$ | 映射覆盖更新（Map Override Update） | 环境状态更新算子，定义为 $(\rho_1 \triangleleft \rho_2)(v) = \rho_2(v)$（若 $v \in \text{dom}(\rho_2)$），否则为 $\rho_1(v)$；用于表达节点提交与栈帧合并时的绑定覆写。 |
