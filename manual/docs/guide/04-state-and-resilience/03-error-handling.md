# 03. 错误处理：异常捕获（`catch`）与主动回滚（`fail`）

Axcutor 将工作流中的异常恢复无缝融入到了结构化控制流图（CFG）中：你不需要学习复杂的异常语法，只需通过 **`catch`** 将运行时异常转化为图的控制分支，或通过 **`-> fail`** 主动触发当前图栈帧的事务回滚。

---

## 一、 四类可恢复运行时异常

除了常规业务分支（`branches`）和不可恢复的引擎级致命终止（如爆栈、取消信号）外，运行时内置了 **4 种可被 `retry` 和 `catch` 处理的可恢复异常选择器**：

| 异常选择器 | 什么时候会触发？ |
| :--- | :--- |
| **`execution`** | 1. `run` 节点的业务执行器报错（如函数抛出异常、API/RPC 失败、脚本非零退出、模型调用失败）；<br>2. 内联 `graph` 或 `use` 子工作流内部主动走向了 `-> fail` 终态。 |
| **`timeout`** | 节点单次尝试耗时超过了 `timeout_ms`。 |
| **`contract`** | 节点执行器返回的数据结构不符合 `output` 的 `TypeSpec` 校验，或返回了未声明的分支名。 |
| **`max_steps`** | 内联 `graph` 或 `use` 子工作流的循环步数超过了其 `graph.max_steps` 上限。 |

---

## 二、 使用 `catch` 将异常物化为控制分支

在任意业务节点（`run`、内联 `graph`、`use: workflow:<path>`）上声明 `catch`，即可将该节点未被 `retry` 消化的异常拦截下来，变成一条或多条控制分支出边：

```yaml
nodes:
  # 形态 A：用 "*" 通配符一站式兜底所有可恢复异常
  query_primary_service:
    timeout_ms: 3000
    retry:
      max_attempts: 2
      on: [execution, timeout]
    catch:
      fallback: "*"                   # 暴露 query_primary_service.fallback 异常出边
    input: [query_key]
    output:
      record: { type: string }
    run: "从主服务查询 variables.query_key，写入 output.record。"

  # 形态 B：按异常类型分流到不同的恢复分支（未列出的异常自动继续向上层栈帧冒泡）
  autonomous_research_subgraph:
    input: [topic]
    output: [report]
    catch:
      bounded_stop: [max_steps, timeout]  # 步数超限或超时 -> 走 .bounded_stop 分支
      exec_failed: [execution]            # 执行报错 -> 走 .exec_failed 分支
    graph:
      max_steps: 20
      nodes: ...
      edges: ...
```

### `catch` 分支的核心回滚与定值规则：
* **零脏写回滚**：当节点进入 `catch.<branch>` 分支时，该节点（或子图）在执行过程中产生的一切半成品修改**全部丢弃**，绝不写入外层图的 `variables`。
* **等价于 `output: []`**：因为进入 `catch` 分支时节点没有产出任何 `output` 变量，如果下游节点需要读取该节点原本负责初始化的变量 `record`，你必须在 `.fallback` 分支连接的补偿节点中为 `record` 赋上兜底值，然后再汇合到下游节点（否则编译期定值检查器会立刻指出 `.fallback` 路径遗漏了 `record` 的赋值）。

---

## 三、 使用 `-> fail` 主动触发图栈帧回滚

在 `graph.edges` 中，除了正常出口 `-> end`（提交输出）外，你还可以将任意节点或分支连向异常回滚终点 **`-> fail`**：

```yaml
edges:
  - start -> fetch_order
  - fetch_order -> verify_amount
  # 当校验发现金额非法时，直接 -> fail：立即中止当前子图、丢弃所有中间修改，并向父节点抛出 execution 异常！
  - verify_amount.invalid -> fail
  - verify_amount.valid -> submit_clearing
  - submit_clearing -> end
```

* 到达 `-> fail` 的路径**不需要**为当前图的 `output` 变量赋值，因为它会直接回滚当前图栈帧并向上抛出 `execution` 异常。

---

## 四、 进阶工程容错配方

将 `timeout_ms`、`retry`、`catch`、`fail` 与内联 `graph` 及 `for_each` 结合，可以直接构建两类强大的生产级容错架构。完整可运行代码请查阅 Cookbook：
1. **[使用 SESE 内联子图构建多步原子事务 `try-catch`](../../cookbook/03-transactions-and-resilience/01-multi-step-atomic-transaction.md)**
2. **[`for_each` 批处理元素级异常隔离与死信队列（DLQ）零拷贝分流](../../cookbook/03-transactions-and-resilience/02-batch-isolation-and-dead-letter-queue.md)**
