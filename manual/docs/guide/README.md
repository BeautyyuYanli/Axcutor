# 渐进式教程（Guide）

本教程按从简到繁的顺序介绍 Axcutor 工作流 DSL 的全部核心概念与原语。每一节聚焦一个正交特性，帮助你快速建立清晰的编排心智模型。

---

## 章节目录

### 第一部分：基础构建块与模块化（`01-foundations/`）
1. **[01. Basics：基础概念与最小工作流](./01-foundations/01-basics.md)**
   万物皆节点模型、根节点函数签名（`input` / `output`）、`run` 作为任意受函数签名约束的业务代码执行体、图级共享变量表 `variables` 以及顺序连边（`start -> a -> b -> end`）。
2. **[02. Use：跨文件调用子工作流](./01-foundations/02-use-subworkflows.md)**
   通过 `use` 调用外部独立工作流或内建算子，使用 `with.input`（支持顶层变量、结构体静态字段路径、标量字面量与 `{ value }` 包装器）和 `with.output` 跨命名空间传递数据。
3. **[03. Inline Graph：内联块级子图（SESE 容器）](./01-foundations/03-inline-graphs.md)**
   在单个文件内部使用内联 `graph` 构建单入口单出口（SESE）块级作用域，利用 `input: [...]` 显式捕获外层变量，并通过 `output: [...]` 列表简写免去重复类型声明。

### 第二部分：条件分支与动态循环（`02-control-flow/`）
1. **[01. Branches：排他条件分支与按需产出](./02-control-flow/01-branches.md)**
   声明 `branches` 与分支关联输出子集（Tagged Union，彻底消除 `null` 占位初值污染）、分支早退出口 `end.<branch>`，以及零开销确定性算子 `builtin:switch` 与 `builtin:unwrap`。
2. **[02. 成环回边：动态条件循环（While Loop）](./02-control-flow/02-cycles-and-back-edges.md)**
   通过 CFG 成环回边表达未知迭代次数的条件收敛循环、跨轮次状态初始化与演进，以及循环内集合弹出与追加算子（`builtin:array_shift`、`builtin:array_append`、`builtin:map_set`）。

### 第三部分：结构化并发编排（`03-concurrency/`）
1. **[01. 同构结构化并发：集合遍历（`for_each`）](./03-concurrency/01-for-each.md)**
   对强类型列表或有序映射执行元素级遍历、单次迭代局部隔离帧（`item`/`key`/`index`）、内部消费迭代级分支，以及三类正交输出通道：`filter`（零拷贝保留原元素）、`collect`（收集提升与 `flatten` 展开）和 `state`（有状态串行折叠）。
2. **[02. 异构结构化并发：Fork-Join 并发组（`-> [a, b] ->`）](./03-concurrency/02-fork-join.md)**
   使用三元并发边同时激活多个异构节点、并发分支的 SESE 约束、Fork 快照隔离与写集互斥保障。

### 第四部分：栈帧作用域、护栏与错误处理（`04-state-and-resilience/`）
1. **[01. 图栈帧与命名空间](./04-state-and-resilience/01-frames-and-scoping.md)**
   理解函数调用栈帧（`Function Frame`）与块级子图栈帧（`Block Frame`）的隔离边界、宿主输入与 `graph.variables` 初值互斥原则、局部私有变量无冲突封装以及分支敏感定值保障。
2. **[02. 执行护栏：`max_steps`、调用栈深度与 `timeout_ms`](./04-state-and-resilience/02-guardrails-and-timeouts.md)**
   通过单图步数上限 `max_steps` 防止动态死循环、通过调用栈深度上限防止无限递归爆栈、通过 `timeout_ms` 与 `retry` 实现单次尝试超时控制与基于只读输入快照的原地重试。
3. **[03. 错误处理：异常捕获（`catch`）与主动回滚（`fail`）](./04-state-and-resilience/03-error-handling.md)**
   三级错误分层模型、四类可恢复运行时异常（`execution` / `timeout` / `contract` / `max_steps`）、使用 `catch` 将异常物化为控制分支（`output: []` 回滚语义）以及使用 `-> fail` 主动触发子图栈帧回滚。
