# Axcutor 全文档站（Manual）

本目录是 Axcutor 工作流 DSL 与运行时引擎的完整文档中心，按受众与工程职责划分为三个顶层部分：

---

## 一、 [用户文档（`docs/`）](./docs/README.md)

**面向工作流编写者、业务集成开发者与自动化 Agent**。不涉及编译器或引擎内部实现算法，聚焦如何理解、编写、组合与查阅 Axcutor 工作流：

* **[1. 渐进式教程（`docs/guide/`）](./docs/guide/README.md)**
  按学习曲线由浅入深讲解每一个核心概念与控制流原语：
  * [01-foundations](./docs/guide/01-foundations/01-basics.md)：基础模型、`run` 广义函数契约、`use` 跨文件调用与内联 `graph` 块级子图
  * [02-control-flow](./docs/guide/02-control-flow/01-branches.md)：`branches` 排他分支（按需产出）与成环回边（`while` 动态循环）
  * [03-concurrency](./docs/guide/03-concurrency/01-for-each.md)：`for_each` 同构集合并发与 `-> [a, b] ->` 异构 Fork-Join 并发组
  * [04-state-and-resilience](./docs/guide/04-state-and-resilience/01-frames-and-scoping.md)：图栈帧与变量作用域、运行护栏（`max_steps` / `timeout_ms` / `retry`）与错误处理（`catch` / `fail`）
* **[2. 实战组合配方（`docs/cookbook/`）](./docs/cookbook/README.md)**
  端到端、多原语正交组合的生产级工作流模板：
  * [01-loops-and-queues](./docs/cookbook/01-loops-and-queues/01-generate-verify-refine-loop.md)：生成-校验-修正闭环、候选节点短路早退回退链
  * [02-collection-and-concurrency](./docs/cookbook/02-collection-and-concurrency/01-stateful-sequential-fold.md)：有状态串行折叠、并发多路分流与扁平化、外层并发遍历 + 内层成环循环
  * [03-transactions-and-resilience](./docs/cookbook/03-transactions-and-resilience/01-multi-step-atomic-transaction.md)：内联子图多步原子事务 `try-catch`、批处理异常隔离与死信队列（DLQ）分流
* **[3. 用户速查字典（`docs/reference/`）](./docs/reference/README.md)**
  日常编写工作流时的语法与参数速查手册：
  * [节点与图字段速查表](./docs/reference/01-node-and-graph-fields.md)、[6 种边语法速查表](./docs/reference/02-edge-syntax-cheatsheet.md)、[TypeSpec 类型写法速查表](./docs/reference/03-typespec-cheatsheet.md)
  * [运行时内建节点字典（`builtins/`）](./docs/reference/builtins/README.md)：[`unwrap`](./docs/reference/builtins/unwrap.md)、[`switch`](./docs/reference/builtins/switch.md)、[`array_shift`](./docs/reference/builtins/array-shift.md)、[`array_append`](./docs/reference/builtins/array-append.md)、[`map_set`](./docs/reference/builtins/map-set.md)

---

## 二、 [语言与运行时形式化规范（`spec/`）](./spec/README.md)

**面向编译器/静态检查器（Validator）、运行时调度引擎（Runtime）与业务执行器插件（Executors）实现者**的权威技术规范（Single Source of Truth）：

* **[01-grammar-and-ast](./spec/01-grammar-and-ast/01-document-and-node-grammar.md)**：文档与节点文法、数据绑定与字面量消歧、CFG 边表达式 AST 构造
* **[02-type-system-and-symbols](./spec/02-type-system-and-symbols/01-typespec-well-formedness.md)**：`TypeSpec` 良构性公理、结构化类型等价与实例兼容性、两阶段符号表决议算法、静态字段路径投影推导与 `builtin:*` 泛型推导规则
* **[03-static-flow-analysis](./spec/03-static-flow-analysis/01-cfg-topology-and-sese-validation.md)**：CFG 拓扑穷尽与 SESE 校验、宿主初值互斥与命名无冲突检查、全路径分支敏感必达定值方程（Definite Assignment）、并发写集互斥公理
* **[04-runtime-execution-model](./spec/04-runtime-execution-model/01-frame-memory-model.md)**：三类栈帧内存模型、Fork 快照隔离与 `for_each` 调度状态机、`max_steps` 精确计步与调用树深度追踪、三级异常与栈展开状态机
* **[05-executor-abi](./spec/05-executor-abi/01-run-executor-contract.md)**：`run` 业务执行器标准 ABI 契约（只读输入快照载荷、输出与分支契约校验、异常映射与多执行器扩展规范）

---

## 三、 [早期原型归档（`prototype/`）](./prototype/)

保留最初阶段的 5 份探索性设计文档作为历史参考：
* [`DSL.md`](./prototype/DSL.md)、[`builtins.md`](./prototype/builtins.md)、[`error_handling.md`](./prototype/error_handling.md)、[`while_and_iter.md`](./prototype/while_and_iter.md)、[`while_plus_iter.md`](./prototype/while_plus_iter.md)
