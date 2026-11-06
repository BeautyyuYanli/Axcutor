# Axcutor 用户文档（User Documentation）

欢迎查阅 Axcutor 工作流用户文档。

Axcutor 采用**单令牌结构化并发控制流图（Structured CFG）**与**严格闭合的静态强类型系统（JSON Schema 子集）**来编排业务流程。在 Axcutor 中，`run` 节点代表**任意满足函数签名约束（`input -> (branch, output)`）的业务代码**（包括本地/远程函数、脚本沙箱、微服务 RPC、数据库操作或大模型调用等），而工作流运行时则自动为你保障类型安全、栈帧隔离、并发无竞争与异常事务回滚。

---

## 文档结构导航

本用户文档分为三个互补的模块，可根据你的当前需求选择阅读路径：

### 1. [渐进式教程（`guide/`）](./guide/README.md) —— **从零掌握核心原语**
按认知递进顺序，用简明直观的示例讲解每一个语言特性：
* **第一部分：基础构建块与模块化**
  * [01. Basics：基础概念与最小工作流](./guide/01-foundations/01-basics.md)
  * [02. Use：跨文件调用子工作流](./guide/01-foundations/02-use-subworkflows.md)
  * [03. Inline Graph：内联块级子图（SESE 容器）](./guide/01-foundations/03-inline-graphs.md)
* **第二部分：条件分支与动态循环**
  * [01. Branches：排他条件分支与按需产出](./guide/02-control-flow/01-branches.md)
  * [02. 成环回边：动态条件循环（While Loop）](./guide/02-control-flow/02-cycles-and-back-edges.md)
* **第三部分：结构化并发编排**
  * [01. 同构结构化并发：集合遍历（`for_each`）](./guide/03-concurrency/01-for-each.md)
  * [02. 异构结构化并发：Fork-Join 并发组（`-> [a, b] ->`）](./guide/03-concurrency/02-fork-join.md)
* **第四部分：栈帧作用域、护栏与错误处理**
  * [01. 图栈帧与命名空间](./guide/04-state-and-resilience/01-frames-and-scoping.md)
  * [02. 执行护栏：`max_steps`、调用栈深度与 `timeout_ms`](./guide/04-state-and-resilience/02-guardrails-and-timeouts.md)
  * [03. 错误处理：异常捕获（`catch`）与主动回滚（`fail`）](./guide/04-state-and-resilience/03-error-handling.md)

### 2. [实战组合配方（`cookbook/`）](./cookbook/README.md) —— **端到端工程模式模板**
演示如何将多个正交原语组合起来解决复杂业务场景：
* **循环与动态队列**：[生成-校验-修正闭环](./cookbook/01-loops-and-queues/01-generate-verify-refine-loop.md) | [候选节点短路早退回退链](./cookbook/01-loops-and-queues/02-short-circuit-fallback-chain.md)
* **集合流水线与嵌套并发**：[有状态串行折叠](./cookbook/02-collection-and-concurrency/01-stateful-sequential-fold.md) | [并发多路分流与扁平化](./cookbook/02-collection-and-concurrency/02-multi-way-triage-and-flatmap.md) | [外层并发遍历 + 内层成环循环](./cookbook/02-collection-and-concurrency/03-concurrent-map-with-inner-loop.md)
* **事务与批处理容错**：[内联子图多步原子事务 `try-catch`](./cookbook/03-transactions-and-resilience/01-multi-step-atomic-transaction.md) | [批处理元素级隔离与死信队列（DLQ）分流](./cookbook/03-transactions-and-resilience/02-batch-isolation-and-dead-letter-queue.md)

### 3. [用户速查字典（`reference/`）](./reference/README.md) —— **日常查字段与内建算子**
* [节点与图字段速查表](./reference/01-node-and-graph-fields.md)
* [6 种控制边语法速查表](./reference/02-edge-syntax-cheatsheet.md)
* [TypeSpec 类型写法速查表](./reference/03-typespec-cheatsheet.md)
* [内建节点字典（`builtins/`）](./reference/builtins/README.md)：[`builtin:unwrap`](./reference/builtins/unwrap.md) | [`builtin:switch`](./reference/builtins/switch.md) | [`builtin:array_shift`](./reference/builtins/array-shift.md) | [`builtin:array_append`](./reference/builtins/array-append.md) | [`builtin:map_set`](./reference/builtins/map-set.md)
