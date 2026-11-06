# 01. CFG 拓扑穷尽、SESE 并发限制与可达性公理

本规范定义控制流图（CFG）在编译期必须通过的拓扑良构性检查（Topological Validation）。

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

对于出现在任意三元并发边 `src -> [W_1, ..., W_k] -> join_target` 中的每个成员节点 $W_i$：

1. **独占拓扑**：$W_i$ 禁止在其他任何边中作为 `SourceEndpoint`、`TargetEndpoint`、`JoinTarget` 或再次出现在其他 `ConcurrentGroup` 中。
2. **单出口无分支限制**：若 $W_i$ 未挂载 `for_each`，则 $W_i$ **禁止对外暴露任何正常 `branches` 或 `catch`**（包括禁止直接使用带分支的 `builtin:switch`、未传 `default` 的 `builtin:unwrap` 及 `builtin:array_shift`）。

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
