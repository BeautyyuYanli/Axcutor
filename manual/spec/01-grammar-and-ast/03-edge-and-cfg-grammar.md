# 03. 控制边表达式与 CFG 语法规范

本规范定义 `graph.edges` 中连边字符串的词法与语法生成式（Production Rules），以及 AST 归一化表示。

---

## 一、 边表达式的形式化文法（EBNF）与词法空白规则

`graph.edges` 必须为非空字符串数组。每条边字符串在完成词法切分后，必须匹配以下 EBNF 文法：

```txt
EdgeExpr        ::= SourceEndpoint "->" TargetEndpoint
                  | SourceEndpoint "->" ConcurrentGroup "->" JoinTarget ;

SourceEndpoint  ::= "start"
                  | NodeId
                  | NodeId "." BranchOrCatchId ;

ConcurrentGroup ::= "[" NodeId ( "," NodeId )+ "]" ;   (* 至少包含 2 个互异的 NodeId *)

TargetEndpoint  ::= NodeId
                  | EndEndpoint
                  | "fail" ;

JoinTarget      ::= NodeId
                  | EndEndpoint ;                      (* Fork-Join 屏障禁止直接指向 "fail" *)

EndEndpoint     ::= "end"
                  | "end" "." HostBranchId ;
```

### 词法空白字符规则（Whitespace Rules）

* **允许水平空白的位置**：整条边字符串的首尾，以及定界符 `->`、`,`、`[`、`]` 的两侧，均允许出现任意数量（0 个或多个）的水平空白字符（ASCII 空格 `U+0020` 或水平制表符 `\t` `U+0009`）。解析器在构建 AST 时自动剥离这些空白字符。
* **禁止空白的位置**：成员访问点号 `.` 的两侧（即 `NodeId.BranchOrCatchId` 与 `end.HostBranchId` 内部）**严禁**出现任何空白字符；`NodeId`、`BranchOrCatchId`、`HostBranchId` 及控制流端点保留字（`start`、`end`、`fail`）内部亦不可被空白字符截断。违反此规则的边字符串直接判定为词法解析错误。
* **禁止换行符**：单条边字符串内禁止包含换行符（`\n` 或 `\r`）。

---

## 二、 AST 归一化表示与语法级良构性约束

语法解析器将每条合法的 `EdgeExpr` 字符串归一化为代数数据类型 `EdgeAst`，消除原始字符串中的空白差异与表层语法糖：

### 1. 形式化 AST 数据结构定义

```txt
type EdgeAst =
  | DirectEdge {
      raw: String,                   (* 原始边声明字符串，用于诊断定位 *)
      source: SourceEndpointAst,
      target: TargetEndpointAst
    }
  | ForkJoinEdge {
      raw: String,                   (* 原始边声明字符串，用于诊断定位 *)
      source: SourceEndpointAst,
      workers: List<NodeId>,         (* 保持声明顺序的并发工作节点列表 *)
      join_target: JoinTargetAst
    }

type SourceEndpointAst =
  | Start                            (* 对应 "start" *)
  | Node(node_id: NodeId)            (* 对应无分支源节点 NodeId *)
  | Branch(
      node_id: NodeId,
      branch_id: BranchOrCatchId     (* 对应 NodeId.BranchOrCatchId，含正常分支与异常捕获分支 *)
    )

type TargetEndpointAst =
  | TargetNode(node_id: NodeId)      (* 对应目标工作节点 NodeId *)
  | End(host_branch: HostBranchId?)  (* 对应 "end" (None) 或 "end.<HostBranchId>" (Some) *)
  | Fail                             (* 对应异常终态 "fail" *)

type JoinTargetAst =
  | TargetNode(node_id: NodeId)      (* 汇聚至下游工作节点 NodeId *)
  | End(host_branch: HostBranchId?)  (* 汇聚至图正常终态 "end" 或 "end.<HostBranchId>" *)
```

### 2. 单跳控制边（`DirectEdge`）的四种细分形态

`DirectEdge(source, target)` 对应 `SourceEndpoint -> TargetEndpoint`，按端点组合涵盖四种标准控制流语义形态：

1. **无分支演进边**：`source` 为 `Start` 或 `Node(u)`，`target` 为 `TargetNode(v)`（即 `start -> target` 或 `node -> target`）。
2. **分支排他路由边**：`source` 为 `Branch(u, choice)`，`target` 为 `TargetNode(v)`（即 `node.choice -> target`）。
3. **正常终态提交边**：`target` 为 `End(None)` 或 `End(Some(branch))`（即 `source -> end` 或 `source -> end.branch`）。
4. **异常终态回滚边**：`target` 为 `Fail`（即 `source -> fail` 或 `source.choice -> fail`）。

### 3. 结构化并发三元边（`ForkJoinEdge`）与语法级良构性断言

`ForkJoinEdge(source, workers, join_target)` 对应 `SourceEndpoint -> [worker_1, worker_2, ...] -> JoinTarget`。在 AST 归一化阶段，解析器与静态校验器强制执行以下语法级良构性断言（Well-formedness Assertions）：

* **保留字隔离断言**：所有出现的标识符 `NodeId`、`BranchOrCatchId`、`HostBranchId` 必须符合受限标识符词法规范（属于 $\text{Identifier} \setminus \mathcal{R}$，严禁使用控制流保留字 `start`、`end`、`fail` 或宿主代码生成保留关键字）。
* **并发基数与互异性断言**：对于 `ForkJoinEdge.workers = [w_1, w_2, ..., w_k]`，必须满足：
  $$\text{length}(\text{workers}) = k \ge 2 \quad \land \quad \forall 1 \le i < j \le k,\; w_i \neq w_j$$
  即并发组至少包含 2 个工作节点，且列表内节点名两两互异（禁止在同一并发组中重复列出同一节点名），同时保留源码中的声明顺序。
* **节点引用存在性断言**：`SourceEndpointAst` 中的 `node_id`、`TargetNode(node_id)` 中的 `node_id`，以及 `ForkJoinEdge.workers` 中的每个 `w_i`，均必须存在于当前图的 `graph.nodes` 键集合中。
* **Join 目标受限断言**：`ForkJoinEdge.join_target` 的类型域严格受限于 `JoinTargetAst`（仅允许 `TargetNode(node_id)` 或 `End(host_branch)`），语法级直接排除了 `-> [a, b] -> fail` 形态。
