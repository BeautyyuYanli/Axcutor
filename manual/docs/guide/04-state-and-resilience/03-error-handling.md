# 03. 错误处理：异常捕获（`catch`）与主动回滚（`fail`）

Axcutor 将异常恢复整合在结构化控制流图（CFG）中：节点可通过 **`catch`** 将未被 `retry` 消化的运行时异常物化为图内控制分支，子图可通过 **`-> fail`** 主动中止当前图栈帧并回滚未提交的图变量状态。

---

## 一、 四类可恢复运行时异常

除常规业务分支（`branches`）与不可捕获的引擎级致命错误（如 `StackOverflowError`、外部取消信号）外，运行时定义了 **4 类可被 `retry` 与 `catch` 处理的可恢复异常选择器**：

| 异常选择器 | 触发条件 |
| :--- | :--- |
| **`execution`** | 1. `run` 节点的业务执行器返回执行错误（如函数抛出未捕获异常、`executor: module` 导入业务模块触发 `ModuleNotFoundError`/`ImportError`、RPC/API 调用失败、脚本非零退出）；<br>2. 内联 `graph` 或 `use` 子工作流内部走向了 `-> fail` 异常终点。 |
| **`timeout`** | 节点单次尝试耗时超过其配置的 `timeout_ms`。 |
| **`contract`** | 节点执行器返回的数据结构未通过 `output` 的 `TypeSpec` 校验（如 `builtin`/`module` 执行器构造 `Output` / `Branch` 触发 `ValidationError`）、返回了非法分支对象，或 `executor: module` 目标模块缺失 `async def execute` 入口 / 伴生契约桩 `__axcutor_contract_sha256__` 指纹校验不匹配。 |
| **`max_steps`** | 内联 `graph` 或 `use` 子工作流的累计调度步数超过其 `graph.max_steps` 上限（注：原子 `run` 节点与顶层 `run` 工作流不存在图步数计数器，禁止在其 `retry.on` 或 `catch` 显式列表中声明 `max_steps`）。 |

---

## 二、 使用 `catch` 将异常物化为控制分支

在业务节点（`run`、内联 `graph`、`use: workflow:<pkg_path>`）上声明 `catch`，可将指定类型的异常拦截并转化为出边 `<node>.<catch_branch>`。

### 1. 通配符兜底与按异常类型分流

* **通配符 `catch.<branch>: "*"`**：捕获所有未被同一 `catch` 块中其他具名选择器匹配的剩余可恢复异常。
* **选择器列表 `catch.<branch>: [<selector>, ...]`**：按异常类别分流至不同的恢复分支；未在 `catch` 中匹配的异常将自动向上层父图栈帧冒泡。

```yaml
nodes:
  # 形态 A：使用 "*" 通配符将所有可恢复异常路由至 query_primary.fallback 出边
  query_primary:
    timeout_ms: 3000
    retry:
      max_attempts: 2
      on: [execution, timeout]
    catch:
      fallback: "*"
    input: [query_key]
    output:
      record: { type: string }
    run:
      executor: builtin
      content: |
        return Output(record=f"Primary record for {input.query_key}")

  # 形态 B：按异常类型分流至不同恢复出边（contract 未列出，发生时直接向父图冒泡）
  autonomous_research_subgraph:
    input: [topic]
    output:
      report: { type: string }
    catch:
      bounded_stop: [max_steps, timeout]  # 暴露 .bounded_stop 出边
      exec_failed: [execution]            # 暴露 .exec_failed 出边
    graph:
      max_steps: 20
      nodes: ...
      edges: ...
```

### 2. `catch` 分支的零脏写回滚与补偿定值

当节点进入 `catch.<branch>` 分支时，运行时保证以下数据流与状态边界语义：

1. **零脏写回滚与外部副作用补偿**：运行时自动丢弃当前节点或子图栈帧中未提交的图变量状态（`variables`），确保半成品数据不会写回外层图。注意自动回滚仅作用于内存中的 `variables`；若前序步骤已对外部系统产生了 I/O 副作用（如预扣外部库存），则应通过 `catch` 分支路由至业务补偿节点完成外部状态冲正。
2. **输出集等价于 `output: []`**：进入 `catch` 分支时，该节点声明的 `output` 变量均未定值。若下游汇合节点需要读取该变量（如 `record`），必须在 `catch` 分支路径上串联补偿节点为其赋兜底值后再汇合：

```yaml
nodes:
  query_primary:
    catch:
      fallback: "*"
    input: [query_key]
    output:
      record: { type: string }
    run:
      executor: builtin
      content: |
        return Output(record=f"Primary:{input.query_key}")

  # 补偿节点：在 .fallback 路径上为 record 提供定值，满足下游 render_view 的必达定值要求
  load_default_record:
    input: [query_key]
    output:
      record: { type: string }
    run:
      executor: builtin
      content: |
        return Output(record=f"Default:{input.query_key}")

  # 汇合节点：无论走主路径还是 .fallback 补偿路径，record 均已定值
  render_view:
    input: [record]
    output:
      html: { type: string }
    run:
      executor: builtin
      content: |
        return Output(html=f"<div>{input.record}</div>")

edges:
  - start -> query_primary
  - query_primary -> render_view
  - query_primary.fallback -> load_default_record
  - load_default_record -> render_view
  - render_view -> end
```

---

## 三、 使用 `-> fail` 主动触发图栈帧回滚

在 `graph.edges` 中，除正常提交出口 `-> end` 外，可将任意节点或分支连向异常回滚终点 **`-> fail`**：

* **回滚当前栈帧未提交的图变量状态**：控制流到达 `-> fail` 时，立即中止当前图栈帧并丢弃其中所有未提交的 `variables` 修改（不会向父图提交任何 `output`）。对于前序节点已产生的外部系统 I/O 副作用，需由父节点通过 `catch` 分支路由至业务补偿节点（如下节示例中的冲正通知节点）执行外部状态冲正。
* **免除 `output` 定值要求**：通往 `-> fail` 的路径无需为当前图的 `output` 变量赋值。
* **向父节点抛出 `execution` 异常**：若当前图为内联 `graph` 或 `use` 子工作流，向父调用节点抛出 `execution` 异常（可被父节点的 `catch` 捕获）；若当前图为顶层根图，则直接以失败状态终止整个工作流。

```yaml
edges:
  - start -> debit_account
  - debit_account -> verify_compliance
  # 当合规校验不通过时，主动 -> fail：丢弃前序 debit_account 在子图栈帧中未提交的 variables 修改，并向父图抛出 execution 异常
  - verify_compliance.invalid -> fail
  - verify_compliance.valid -> commit_ledger
  - commit_ledger -> end
```

---

## 四、 完整示例：多步子图回滚（`-> fail`）与外层补偿（`catch`）

以下工作流使用内联 `graph` 封装多步订单结算：若子图内部库存预扣或金额审核失败，通过 `-> fail` 自动回滚子图栈帧中未提交的 `variables`；外层主图通过 `catch` 捕获异常后路由至补偿节点 `notify_transaction_cancelled`，冲正外部预占资源并发送取消通知：

```yaml
# 1. 根节点输入与输出契约
input:
  order_id: { type: string }
  amount: { type: number }
output:
  settlement_receipt: { type: string }
  cancel_notice: { type: string }

# 2. 根节点排他分支：成功结算产出 settlement_receipt，回滚取消产出 cancel_notice
branches:
  settled:
    description: "结算事务提交成功"
    output: [settlement_receipt]
  cancelled:
    description: "结算事务回滚并发送取消通知"
    output: [cancel_notice]

graph:
  nodes:
    # 阶段 1：使用内联 graph 封装多步结算，挂载 catch 拦截执行失败或子图主动 -> fail
    settlement_transaction:
      input: [order_id, amount]
      output:
        settlement_receipt: { type: string }
      catch:
        tx_aborted: [execution, timeout]
      graph:
        nodes:
          # 步骤 1.1：预扣外部库存，产出子图内部凭证 reservation_id
          reserve_inventory:
            timeout_ms: 3000
            retry:
              max_attempts: 2
              on: [execution, timeout]
            input: [order_id]
            output:
              reservation_id: { type: string }
            run:
              executor: builtin
              content: |
                return Output(reservation_id=f"RES-{input.order_id}")

          # 步骤 1.2：校验结算金额合规性
          audit_payment_limit:
            input: [order_id, amount, reservation_id]
            branches:
              approved: "金额合规，允许扣款"
              reject: "金额超限，触发子图回滚"
            run:
              executor: builtin
              content: |
                if 0.0 < input.amount <= 10000.0:
                    return Branch.approved()
                return Branch.reject()

          # 步骤 1.3：仅在 approved 分支下执行最终扣款并生成 settlement_receipt
          charge_and_issue_receipt:
            input: [order_id, amount, reservation_id]
            output:
              settlement_receipt: { type: string }
            run:
              executor: builtin
              content: |
                return Output(
                    settlement_receipt=f"SETTLED:{input.reservation_id}:{input.amount:.2f}"
                )

        # 子图内部控制流：reject 分支走向 fail，回滚子图未提交的 variables 并向父节点抛出 execution 异常
        edges:
          - start -> reserve_inventory
          - reserve_inventory -> audit_payment_limit
          - audit_payment_limit.reject -> fail
          - audit_payment_limit.approved -> charge_and_issue_receipt
          - charge_and_issue_receipt -> end

    # 阶段 2（业务补偿节点）：当 settlement_transaction 进入 .tx_aborted 分支时冲正外部预占资源并通知
    notify_transaction_cancelled:
      input: [order_id]
      output:
        cancel_notice: { type: string }
      run:
        executor: builtin
        content: |
          return Output(cancel_notice=f"Order {input.order_id} cancelled and rolled back.")

  # 3. 主图控制流连边：正常路径走向 end.settled，异常捕获路径经补偿节点走向 end.cancelled
  edges:
    - start -> settlement_transaction
    - settlement_transaction -> end.settled
    - settlement_transaction.tx_aborted -> notify_transaction_cancelled
    - notify_transaction_cancelled -> end.cancelled
```

> 更多生产级事务与批处理容错模板可参阅 Cookbook：
> * [使用 SESE 内联子图构建多步原子事务 `try-catch`](../../cookbook/03-transactions-and-resilience/01-multi-step-atomic-transaction.md)
> * [`for_each` 批处理元素级异常隔离与死信队列（DLQ）分流](../../cookbook/03-transactions-and-resilience/02-batch-isolation-and-dead-letter-queue.md)
