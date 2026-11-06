# 实战组合配方（Cookbook）

本目录提供端到端、多原语正交组合的生产级 Axcutor 工作流模板。每个配方针对一类典型的工程编排问题，展示如何组合分支、成环回边、`for_each`、内联 `graph`、`builtin:*` 算子与异常处理原语。

---

## 配方场景索引

### 一、 循环收敛与动态队列（`01-loops-and-queues/`）
* **[01. 生成-校验-修正闭环（CFG 回边 + 分支关联输出）](./01-loops-and-queues/01-generate-verify-refine-loop.md)**
  * **适用场景**：自动生成代码/SQL/配置 → 沙箱执行验证 → 若失败则携带报错信息回边修正，最多重试 $N$ 次；成功出口与失败出口各自按需产出不同字段，无需 `null` 兜底初值。
* **[02. 候选节点短路早退遍历与失败轨迹记录（`array_shift` + `array_append`）](./01-loops-and-queues/02-short-circuit-fallback-chain.md)**
  * **适用场景**：按优先级顺序依次尝试多个候选服务节点或检索源，**一旦某个候选成功立即短路早退返回结果**（不再尝试剩余节点），同时用 `array_append` 记录所有前置失败轨迹。

### 二、 集合流水线与嵌套并发（`02-collection-and-concurrency/`）
* **[01. 可选配置拆包 + 跨分片有状态串行折叠（`unwrap` + `for_each.state`）](./02-collection-and-concurrency/01-stateful-sequential-fold.md)**
  * **适用场景**：先用 `builtin:unwrap` 检查并拆包可选的全局初始上下文，再按序逐个处理分片/章节，使第 $i$ 步读取并更新第 $i-1$ 步累积的全局状态（`state`），同时收集每一步的分片产出（`collect`）。
* **[02. 并发多路分流、条件收集与扁平化汇总（`filter` + `collect` + `flatten`）](./02-collection-and-concurrency/02-multi-way-triage-and-flatmap.md)**
  * **适用场景**：高并发遍历一批输入项，按评估分支零拷贝保留高优原始结构体（`filter`）、自动过滤无效项仅收集有效产出（条件 `collect`），并将每项提取的标签子列表扁平化拼接为一维总表（`flatten: true`）。
* **[03. 外层 `for_each` 并发遍历 + 内层 SESE 子图 `while` 循环构建/审校](./02-collection-and-concurrency/03-concurrent-map-with-inner-loop.md)**
  * **适用场景**：外层并发处理多个章节或服务模块，而**每个模块内部需要独立运行多轮“编译/翻译 → 审校/测试 → 打回重做”的 `while` 循环**，且各并发模块的内部循环计数器与中间草稿完全隔离。

### 三、 事务回滚与批处理容错（`03-transactions-and-resilience/`）
* **[01. 使用 SESE 内联子图构建多步原子事务 `try-catch` 与补偿路由](./03-transactions-and-resilience/01-multi-step-atomic-transaction.md)**
  * **适用场景**：将多个串联步骤包裹在内联 `graph` 子图中共享总超时与异常边界；内部任意一步报错、超时或因业务校验不合法主动 `-> fail` 时，自动原子回滚整个子图的未提交状态，并在外层 `catch` 分支统一执行入队补偿。
* **[02. `for_each` 批处理元素级超时重试、异常隔离与死信队列（DLQ）分流](./03-transactions-and-resilience/02-batch-isolation-and-dead-letter-queue.md)**
  * **适用场景**：大批量并发执行任务时，单个元素超时仅对该元素原地重试；重试后仍超时或崩溃的元素被 `catch` 拦截并由 `for_each.filter` 零拷贝分流到对应的死信队列变量中，绝不中断整批中其他正常元素的收集。
