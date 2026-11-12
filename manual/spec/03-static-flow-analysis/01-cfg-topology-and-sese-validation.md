# 01. CFG 拓扑穷尽、SESE 并发限制与可达性公理

本规范定义控制流图（CFG）在编译期必须通过的拓扑良构性检查（Topological Validation）以及跨工作流包调用图（Workflow Package Call Graph）、显式根包挂载单元（`MountUnit`）与 `_` 前缀私有可见性的模块化校验准则。

---

## 一、 单令牌确定性出边穷尽与唯一性公理

1. **图起点 `start`**：
   * 在 `graph.edges` 中，`start` 必须且只能作为 **1 条**边的 `SourceEndpoint`。
2. **非并发组成员节点 $N$（未出现在任何 `[worker_1, ...]` 列表中）**：
   * **情况 A（$N$ 挂载了 `for_each`）**：无论 $N$ 内部是否声明了 `branches` 或 `catch`，其所有分支均由 `for_each` 内部消费；在外层 `graph.edges` 中，$N$ 必须且只能有 **1 条**无后缀默认出边 `N -> ...`，禁止出现 `N.<branch> -> ...`。
   * **情况 B（$N$ 未挂载 `for_each`，且无正常 `branches`、无 `catch`）**：必须且只能有 **1 条**无后缀默认出边 `N -> ...`。
   * **情况 C（$N$ 未挂载 `for_each`，无正常 `branches`，但声明了 `catch`）**：
     * 必须有且仅有 **1 条**无后缀正常出边 `N -> ...`；
     * 同时对每个 $c \in \text{dom}(N.\text{catch})$，必须有且仅有 **1 条**异常出边 `N.c -> ...`。
   * **情况 D（$N$ 未挂载 `for_each`，且声明了非空正常 `branches`）**：
     * 禁止出现无后缀出边 `N -> ...`；
     * 对每个正常分支 $b \in \text{dom}(N.\text{branches})$，必须有且仅有 **1 条**出边 `N.b -> ...`；
     * 对每个异常分支 $c \in \text{dom}(N.\text{catch})$（若有），必须有且仅有 **1 条**出边 `N.c -> ...`。

---

## 二、 并发组 `[worker_1, ..., worker_k]` 的 SESE 限制公理

对于出现在任意三元并发边 $e = (\text{src} \to [W_1, \dots, W_k] \to \text{join\_target})$ 中的每个成员节点 $W_i \in \{W_1, \dots, W_k\}$：

1. **独占拓扑（含本条并发边自身端点互斥）**：
   * **本边首尾互斥**：$W_i$ 禁止作为本条并发边 $e$ 自身的源节点或汇合目标节点：
     $$W_i \neq \text{NodeOf}(\text{src}) \;\land\; W_i \neq \text{join\_target}$$
     *(例如禁止书写 `w1 -> [w1, w2] -> next_node` 或 `prev_node -> [w1, w2] -> w1`。)*
   * **跨边完全独占**：在当前图其余所有边 $e' \in G.\text{edges} \setminus \{e\}$ 中，$W_i$ 禁止作为 `SourceEndpoint`、`TargetEndpoint`、`JoinTarget` 或再次出现在其他 `ConcurrentGroup` 中。
2. **单出口无分支限制**：若 $W_i$ 未挂载 `for_each`，则 $W_i$ 的静态正常分支集合与异常分支集合必须均为空：
   $$\text{Branches}(W_i) = \emptyset \;\land\; \text{CatchBranches}(W_i) = \emptyset$$
   即 $W_i$ **禁止对外暴露任何正常 `branches` 或 `catch`**（例如禁止直接放置 `builtin:switch`、分支模式的 `builtin:compare` / `builtin:logic` 或未配置 `default` 兜底值而暴露异常分支的内建算子）。

---

## 三、 终点契约匹配与全图双向可达性定理

1. **宿主终点分支匹配**：
   * 若当前图宿主未声明 `branches`（即 `branches` 为空），图内所有正常终态边必须指向无后缀的 `end`，禁止出现 `end.<branch>`。
   * 若当前图宿主声明了非空 `branches`，图内禁止出现无后缀的 `end`，所有正常终态边必须指向合法的 `end.<branch>`（其中 `<branch>` $\in \text{dom}(\text{host.branches})$）。
2. **前向可达性（No Dead Code）**：
   * 图内每一个节点 $N \in \text{dom}(G.\text{nodes})$ 必须存在至少一条从 `start` 出发的有向路径到达 $N$。
3. **后向出口可达性（No Inescapable Cycles / Must Have Normal Exit）**：
   * **全局正常出口存在性**：整张图必须至少存在一条从 `start` 到达 `end`（或某个 `end.<branch>`）的有向路径（不允许全图仅有 `fail` 而无 `end`）。
   * **节点终态可达性**：图内每一个节点 $N \in \text{dom}(G.\text{nodes})$ 必须至少存在一条从 $N$ 出发到达终态（`end`、`end.<branch>` 或 `fail`）的有向路径。
   * **环路可退性（无死循环）**：对于图中的每一个非平凡强连通分量（SCC / 环路），必须至少存在一条跳出该环路并通向终态（`end`、`end.<branch>` 或 `fail`）的退环路径。

---

## 四、 跨工作流包调用图、显式根包挂载单元（`MountUnit`）与私有可见性公理

当编译器解析编译请求及图内 `CallNode`（`use: workflow:<PackagePath>`）时，遵循以下根命名空间挂载、资源头文件声明、可见性与递归调用公理：

1. **显式无遮蔽根包挂载单元与唯一入口保留字（`MountUnit` & Reserved `"__main__"`）**：
   * **闭合编译请求与 `MountUnit`**：宿主环境向编译器提供闭合快照请求 `{ entry, mounts }`，其中 `mounts: Map<RootPackageName | "__main__", MountUnit>` 按顶层根名组织。每个挂载单元 `MountUnit` 为仅包含以下三个字段的闭合对象：
     * `packages?: Map<PackagePath | "__main__", SourceDocument>`（默认 `{}`）：该根命名空间下的 `__axcutor__.yaml` 工作流源文档表；
     * `resources?: Map<PackagePath, SourceDocument>`（默认 `{}`）：该根命名空间下的 `__ax_resources__.yaml` 静态资源声明头文件表；
     * `allowed_executors?: List<Identifier> | null`（默认 `null`）：该根下工作流允许使用的 `run.executor` 白名单（`null` 表示不限制，`[]` 表示禁止全部 `run` 节点；详见 [`01-run-executor-contract.md`](../05-executor-abi/01-run-executor-contract.md)）。
   * **根命名空间归属与无遮蔽隔离**：
     * 具名挂载根键 `RootPackageName` 必须为公开包路径段（$\text{PublicSegment} \neq \texttt{"\_\_main\_\_"}$，违反时报 `schema.mount_root`），且 `mounts` 中严禁出现重复根键；
     * 在具名根 $R$ 的 `MountUnit` 中，`packages` 与 `resources` 的每个键均必须为合法的 `PackagePath`，且其首段必须严格等于 $R$（即 $k = R \;\lor\; \text{starts\_with}(k, R \mathbin{\Vert} \texttt{"."})$，违反时报 `schema.package_path`），任何挂载单元均无法向其他根命名空间注入或遮蔽包定义；
     * `"__main__"` 为系统保留的独立匿名入口标识，其 `MountUnit` 仅允许包含 `packages["__main__"]` 且 `resources` 必须为空；`"__main__"` 不属于可被调用的 `PackagePath`，严禁在 `use: workflow:...` 中引用。
   * **静态资源头文件（`__ax_resources__.yaml`）良构性**：
     * `MountUnit.resources` 中的每份源文档必须为仅包含顶层键 `resources: Map<PublicSegment, ResourceSpec | null>` 的非空闭合映射。`ResourceSpec` 仅允许可选字段 `description: String` 与 `locator: TypeSpec`；公开 `locator` 须满足[TypeSpec 良构性规范](../02-type-system-and-symbols/01-typespec-well-formedness.md)规定的顶层非空纯值约束；
     * 编译器在阶段 1 遍历全部挂载单元的 `resources` 构建全局资源符号表 $\Sigma_{\text{res}} = \{ \texttt{"}\langle\text{ModulePath}\rangle\texttt{:}\langle\text{PublicClassName}\rangle\texttt{"} \}$，无需导入任何 Python 模块即可静态核验所有 `handle` `kind`。编译结果的 `WorkflowBundle.resources` 保留可达工作流实际使用的 `HandleKind -> ResourceSpec` 元数据，包括公开定位符 Schema 与说明；未使用的声明不进入结果。
2. **基于 `_` 前缀的编译期硬性私有可见性边界（Compile-Time Package & Resource Visibility）**：
   * 设目标包路径（被调用工作流包路径或句柄资源的 `ModulePath`）为 $T = s_1 . s_2 . \dots . s_k \in \text{PackagePath}$（其中首段 $s_1 \in \text{PublicSegment}$）：
     * 若 $\forall i \in \{1, \dots, k\},\; s_i \in \text{PublicSegment}$（所有路径段均不以 `_` 开头），则 $T$ 为**公开包（Public Package）**，可被任意工作流及外部匿名入口 `"__main__"` 访问；
     * 若存在某个路径段 $s_m$（$2 \le m \le k$）以 `_` 开头（$s_m \in \text{PrivateSegment}$，如 `billing._risk_score` 或 `billing.checkout._retry_step`），则 $T$ 为归属于其直接父包前缀 $P_{\text{owner}} = s_1 . \dots . s_{m-1}$ 的**内部私有包（Private Package）**。
   * **可见性判定谓词 $\text{Visible}(C, T)$**：设当前工作流标识为 $C \in \text{PackagePath} \cup \{\texttt{"\_\_main\_\_"}\}$，$C$ 允许访问目标包路径 $T$ 当且仅当对 $T$ 中每一个以 `_` 开头的私有段 $s_m$（$2 \le m \le k$，对应父包前缀 $P_{\text{owner}} = s_1 . \dots . s_{m-1}$），$C$ 均等于 $P_{\text{owner}}$ 或位于 $P_{\text{owner}}$ 的子包树内：
     $$\text{Visible}(C, T) \iff \forall m \in \{2, \dots, k\},\; \Big( \text{starts\_with}(s_m, \texttt{"\_"}) \implies \big( C = P_{\text{owner}} \;\lor\; \text{starts\_with}(C, P_{\text{owner}} \mathbin{\Vert} \texttt{"."}) \big) \Big)$$
   * **违例诊断**：
     * 若跨包调用 `use: workflow:<T>` 不满足 $\text{Visible}(C, T)$，编译器在 `use` 字段处报告静态错误 **`visibility.private_workflow`**（编译请求亦禁止将含私有段的包路径作为顶层 `entry` 启动）；
     * 若工作流 $C$ 声明的 `handle` `kind` 为 `<M>:<Class>` 且不满足 $\text{Visible}(C, M)$，编译器在对应 `TypeSpec` 处报告静态错误 **`visibility.private_resource`**。
3. **基于顶层签名的两阶段声明式解析**：
   * 每个工作流包在顶层显式声明了完备的静态函数签名 $(I_{\text{target}}, O_{\text{target}}, B_{\text{target}}) = (\text{target.input}, \text{target.output}, \text{target.branches})$；
   * 静态检查器先校验全部挂载单元的资源声明并构建 $\Sigma_{\text{res}}$，再沿 `entry` 的调用依赖闭包收集各工作流包的顶层签名表（不依赖包声明顺序，亦不触发任何 Python 模块导入或顶层代码求值），在校验调用节点 $N$ 时仅依赖被调用包的顶层静态签名验证 `with.input`、`with.output` 及外层分支出边的合法性，无需在编译期展开被调用方的内部图体。
4. **允许直接或间接递归调用**：
   * 跨工作流包调用图允许存在直接递归（如工作流包 `math.fact` 内部某条分支通过 `use: workflow:math.fact` 调用自身）或相互递归环路（$A \to B \to A$）；
   * 运行期由 [`03-guardrails-and-step-accounting.md`](../04-runtime-execution-model/03-guardrails-and-step-accounting.md) 定义的调用栈深度护栏 $\text{MAX\_CALL\_DEPTH}$（默认 $64$）强制保证有界终止，超限时抛出不可捕获的 `StackOverflowError`。
