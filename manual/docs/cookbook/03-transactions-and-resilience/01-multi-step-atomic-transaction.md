# 配方 01：使用 SESE 内联子图构建多步原子事务 `try-catch` 与补偿路由

## 一、 场景与需求

在清算或订单履约流程中，包含三个连续步骤：`fetch_order`（拉取订单金额） → `verify_amount`（校验金额合法性） → `submit_clearing`（提交清算接口），需满足两项容错要求：

1. **多步共享超时与原子回滚边界**：三个步骤共享 15 秒总超时上限（`timeout_ms: 15000`）与统一的异常恢复边界；任意一步接口报错、超时，或中间校验判定金额非法（`verify_amount.invalid`），均立即丢弃子图内的所有半成品状态。
2. **统一异常捕获与补偿定值**：子图回滚后，外层通过 `catch` 分支转入补偿节点 `enqueue_retry_job` 将订单写入延迟重试队列，并从 `end.deferred` 分支退出。

---

## 二、 核心原语组合要点

1. **内联 `graph` 事务封装**：将三步流程封装在内联 `graph` 节点 `settlement_transaction` 中，在该节点上声明 `timeout_ms: 15000` 与 `catch: { tx_failed: [execution, timeout, contract] }`。
2. **子图内部主动回滚（`-> fail`）**：当 `verify_amount` 走向 `.invalid` 分支时，通过边 `verify_amount.invalid -> fail` 主动触发子图回滚，向父节点抛出 `execution` 异常。
3. **外层 `catch` 补偿路由**：父图通过 `settlement_transaction.tx_failed -> enqueue_retry_job` 执行补偿并为 `status_note` 赋值，随后走向 `end.deferred`：

```yaml
# 子图内校验失败时主动走向 fail，触发子图回滚并抛出 execution 异常
edges:
  - start -> fetch_order
  - fetch_order -> verify_amount
  - verify_amount.invalid -> fail
  - verify_amount.valid -> submit_clearing
  - submit_clearing -> end
```

---

## 三、 完整工作流定义

```yaml
# 1. 根节点输入与输出契约
input:
  order_id: { type: string }
output:
  settlement_id: { type: string }
  status_note: { type: string }

# 2. 根节点排他分支契约：成功清算产出两个字段，延迟排队仅产出 status_note
branches:
  settled:
    description: "清算完成"
    output: [settlement_id, status_note]
  deferred:
    description: "外部依赖故障、超时或校验异常，已转入延迟队列"
    output: [status_note]

graph:
  nodes:
    # 阶段 1：SESE 内联子图作为多步原子事务边界，共享 15 秒总超时与 catch 拦截器
    settlement_transaction:
      timeout_ms: 15000
      input: [order_id]
      output:
        settlement_id: { type: string }
        status_note: { type: string }
      catch:
        tx_failed: [execution, timeout, contract]
      graph:
        nodes:
          # 事务步骤 1.1：拉取订单金额，配置单步 3 秒超时与最多 3 次原地重试
          fetch_order:
            timeout_ms: 3000
            retry:
              max_attempts: 3
              backoff_ms: 500
              on: [execution, timeout]
            input: [order_id]
            output:
              order_amount: { type: number }
            run: "调用订单服务获取 variables.order_id 的金额，写入 output.order_amount。"

          # 事务步骤 1.2：校验金额区间合法性
          verify_amount:
            input: [order_amount]
            branches:
              valid: "金额合法"
              invalid: "金额为负数或超出允许区间"
            run: "校验 variables.order_amount 是否大于零且在合理区间，选择 valid 或 invalid。"

          # 事务步骤 1.3：提交清算，产出子图对外导出的 settlement_id 与 status_note
          submit_clearing:
            timeout_ms: 5000
            input: [order_id, order_amount]
            output:
              settlement_id: { type: string }
              status_note: { type: string }
            run: "调用清算服务提交 variables.order_id 与 variables.order_amount，写入 output.settlement_id 与 output.status_note。"

        # 子图内部控制流：invalid 分支连向 fail，丢弃 order_amount 并向父节点抛出 execution 异常
        edges:
          - start -> fetch_order
          - fetch_order -> verify_amount
          - verify_amount.invalid -> fail
          - verify_amount.valid -> submit_clearing
          - submit_clearing -> end

    # 阶段 2（异常补偿节点）：当事务子图进入 .tx_failed 分支时，为 status_note 提供定值
    enqueue_retry_job:
      input: [order_id]
      output:
        status_note: { type: string }
      run: "将订单 variables.order_id 加入延迟重试队列，并将排队说明写入 output.status_note。"

  # 3. 主图控制流连边
  edges:
    - start -> settlement_transaction
    - settlement_transaction -> end.settled
    - settlement_transaction.tx_failed -> enqueue_retry_job
    - enqueue_retry_job -> end.deferred
```
