# 03. 控制边表达式与 CFG 语法规范

本规范定义 `graph.edges` 中连边字符串的词法与语法生成式（Production Rules），以及 AST 归一化表示。

---

## 一、 边表达式的形式化文法（EBNF）

`graph.edges` 必须为非空字符串数组。每条边字符串在去除首尾空白及 `->` 两侧空白后，必须严格匹配以下 EBNF 文法：

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
                  | EndEndpoint ;                      (* Fork-Join 屏障严禁直接指向 "fail" *)

EndEndpoint     ::= "end"
                  | "end." HostBranchId ;
```

---

## 二、 6 种标准边 AST 构造与语法级约束

语法解析器将每条边解析为以下两类 AST 边节点之一：

### 1. 单跳控制边（`DirectEdge`）
对应 `SourceEndpoint -> TargetEndpoint`，包含四种细分形态：
1. **无分支演进边**：`start -> target` 或 `node -> target`
2. **分支排他路由边**：`node.choice -> target`
3. **正常终态提交边**：`source -> end` 或 `source -> end.branch`
4. **异常终态回滚边**：`source -> fail` 或 `source.choice -> fail`

### 2. 结构化并发三元边（`ForkJoinEdge`）
对应 `SourceEndpoint -> [worker_1, worker_2, ...] -> JoinTarget`：
* **成员数量与去重约束**：并发组列表 `[worker_1, ..., worker_k]` 必须满足 $k \ge 2$，且所有 `worker_i` 必须两两互异（严禁在同一并发组中重复列出同一节点名）。
* **引用存在性**：每个 `worker_i` 必须存在于当前图的 `graph.nodes` 字典中（严禁包含 `start`、`end`、`fail`）。
* **Join 目标约束**：`JoinTarget` 只能是当前图中的普通节点 `NodeId` 或正常终态 `end` / `end.<branch>`（严禁写 `-> [a, b] -> fail`）。
