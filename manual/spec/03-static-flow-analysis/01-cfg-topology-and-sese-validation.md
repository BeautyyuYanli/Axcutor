# 01. CFG 拓扑穷尽、SESE 并发限制与可达性公理

本规范定义控制流图（CFG）在编译期必须通过的拓扑良构性检查（Topological Validation）以及跨文件调用图（Workflow Call Graph）的模块化校验准则。

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
2. **单出口无分支限制**：若 $W_i$ 未挂载 `for_each`，则 $W_i$ **禁止对外暴露任何正常 `branches` 或 `catch`**（包括禁止直接使用带分支的 `builtin:switch`、未传 `default` 的 `builtin:unwrap`、未传 `default` 的 `builtin:map_get` 及 `builtin:array_shift`）。

---

## 三、 终点契约匹配与全图双向可达性定理

1. **宿主终点分支匹配**：
   * 若当前图宿主未声明 `branches`（即 `branches` 为空），图内所有正常终态边必须指向无后缀的 `end`，禁止出现 `end.<branch>`。
   * 若当前图宿主声明了非空 `branches`，图内禁止出现无后缀的 `end`，所有正常终态边必须指向合法的 `end.<branch>`（其中 `<branch>` $\in \text{dom}(\text{host.branches})$）。
2. **前向可达性（No Dead Code）**：
   * 图内每一个节点 $N \in \text{dom}(G.\text{nodes})$ 必须存在至少一条从 `start` 出发的有向路径到达 $N$。
3. **后向正常出口可达性（No Inescapable Cycles / Must Have Normal Exit）**：
   * 整张图必须至少存在一条从 `start` 到达 `end`（或某个 `end.<branch>`）的路径（不允许全图仅有 `fail` 而无 `end`）。
   * 图内每一个节点 $N \in \text{dom}(G.\text{nodes})$（包含处于环路中的节点），在图拓扑上必须至少存在一条从 $N$ 出发到达 `end`（或某个 `end.<branch>`）或 `fail` 的有向路径；且对于每个强连通分量（SCC / 环路），必须至少存在一条跳出该环路通向终态的出边。

---

## 四、 跨文件工作流调用图（Workflow Call Graph）与受护栏保护的递归

当图内 `CallNode` 通过 `use: workflow:<path>` 调用外部工作流文件时：

1. **基于顶层签名的模块化独立校验**：
   * 每个工作流文件在文件顶层显式声明了完备的静态函数签名 $(I_{\text{target}}, O_{\text{target}}, B_{\text{target}}) = (\text{target.input}, \text{target.output}, \text{target.branches})$；
   * 静态检查器在校验调用节点 $N$ 时，仅依赖被调用工作流的顶层静态签名验证 `with.input`、`with.output` 及外层分支出边的合法性，无需在编译期展开被调用方的内部图体。
2. **允许直接或间接递归调用**：
   * 跨文件工作流调用图允许存在直接递归（如工作流 $A$ 内部某条分支通过 `use: workflow:A` 调用自身）或相互递归环路（$A \to B \to A$）；
   * 运行期由 [`03-guardrails-and-step-accounting.md`](../04-runtime-execution-model/03-guardrails-and-step-accounting.md) 定义的调用栈深度护栏 $\text{MAX\_CALL\_DEPTH}$（默认 $64$）强制保证有界终止，超限时抛出不可捕获的 `StackOverflowError`。
