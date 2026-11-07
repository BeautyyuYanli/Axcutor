# 02. 6 种控制边语法速查表

`graph.edges` 定义控制令牌（Control Token）在图内的流转拓扑。本页汇总支持的 6 种标准边表达式及静态拓扑检查规则。

---

## 一、 6 种标准边语法表

| 序号 | 边表达式语法 | 适用源节点（`source`） | 适用目标（`target`） | 控制流与状态语义 |
| :--- | :--- | :--- | :--- | :--- |
| **1** | `source -> target` | `start`、无分支节点、挂载了 `for_each` 的节点 | 图内普通节点 `<node_id>` | 源节点正常完成后，提交 `output` 并移交控制令牌至 `target`。 |
| **2** | `source.choice -> target` | 未挂载 `for_each` 的有分支节点（含 `branches`、`catch` 或带分支的 `builtin:*`） | 图内普通节点 `<node_id>` | 排他分支路由：走向正常分支 `.choice`（携带该分支关联的 `output` 子集）或异常分支 `.<catch_branch>`（不产出 `output`）时移交 `target`。 |
| **3** | `source -> [worker_a, worker_b] -> join_target` | `start`、无分支节点、挂载了 `for_each` 的节点 | 普通节点、`end`、`end.branch` | 无分支触发 Fork-Join 异构并发组：锁定 Fork 快照并同时激活组内全部节点，全部完成后在屏障处原子合并输出并移交 `join_target`。 |
| **4** | `source.choice -> [worker_a, worker_b] -> join_target` | 未挂载 `for_each` 的有分支节点 | 普通节点、`end`、`end.branch` | 分支触发 Fork-Join 异构并发组：当 `source` 走向 `.choice` 分支时激活并发组，全部完成后汇合移交 `join_target`。 |
| **5** | `source -> end`<br>`source.choice -> end.branch` | 图内任意节点或分支 | `end`（宿主无 `branches` 时）<br>`end.<branch>`（宿主有 `branches` 时） | 正常结束当前图栈帧，提取该出口分支关联的宿主 `output` 变量原子提交给父调用方。 |
| **6** | `source -> fail`<br>`source.choice -> fail` | 图内任意节点或分支 | `fail` | 异常中止当前图栈帧，丢弃当前图对宿主 `output` 的一切未提交修改（Rollback），并向父栈帧抛出 `execution` 异常。 |

```yaml
edges:
  # 1. 无分支单跳边
  - start -> validate_input
  # 6. 主动异常回滚边
  - validate_input.invalid -> fail
  # 4. 分支触发 Fork-Join 并发边
  - validate_input.valid -> [enrich_user, check_quota] -> evaluate_order
  # 2. 分支排他路由边
  - evaluate_order.retry -> validate_input
  # 5. 分支正常出口边
  - evaluate_order.approved -> end.approved
  - evaluate_order.rejected -> end.rejected
```

---

## 二、 连边静态检查规则（Checklist）

1. **单令牌唯一出边**：
   * `start`、非并发组内的无分支节点（含挂载了 `for_each` 的节点），必须且只能有 **1 条**出边。
   * 未挂载 `for_each` 的节点声明的每一个正常分支 `node.<choice>` 和每一个异常分支 `node.<catch_branch>`，均必须且只能有 **1 条**出边（穷尽且唯一）。
2. **`for_each` 节点外层单出口**：
   * 挂载了 `for_each` 的节点，其 `branches` 与 `catch` 在单次迭代内部被消费，在外层 `graph.edges` 中只能书写无后缀的 `node -> target`，不可书写 `node.<choice> -> target`。
3. **并发组 `[worker_a, worker_b]` 成员限制**：
   * 并发组内的节点不可在 `edges` 中额外编写独立的入边或出边；
   * 并发组内的节点（若未挂载 `for_each`）不可直接对外暴露 `branches` 或 `catch`，禁止直接使用任何暴露控制分支的内建算子（如 `builtin:switch`、`builtin:array_shift` 或未传 `default` / 省略 `with.output` 的条件算子），且各成员写回外层图的目标变量集合必须两两互斥。
4. **全图双向可达性**：
   * 图内所有节点必须可从 `start` 到达；
   * 图内所有节点（包含处于成环回边中的节点）必须至少存在一条有向路径能够到达 `end`（或 `end.<branch>`）。
