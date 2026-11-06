# 配方 01：使用 SESE 内联子图构建多步原子事务 `try-catch` 与补偿路由

## 场景与问题

在清算或订单履约流程中，有三个连续步骤：`fetch_order`（拉取订单金额） → `verify_amount`（沙箱校验金额合法性） → `submit_clearing`（提交清算接口）。
* 我们希望这三个步骤共享一个 **15 秒总超时上限** 和一个统一的 **事务异常恢复边界**；
* 如果中间校验发现金额不合法（`verify_amount.invalid`），或者任意一步接口超时/报错，都应当**丢弃子图内的所有半成品状态**，转入补偿节点 `enqueue_retry_job` 将订单放入延迟重试队列，并从 `end.deferred` 退出。

## 解决方案要点

* 将三步操作封装在一个内联 `graph` 节点 `settlement_transaction` 中，并在该内联 `graph` 节点上配置 `timeout_ms: 15000` 与 `catch: { tx_failed: [execution, timeout, contract] }`；
* 在子图内部，当 `verify_amount` 走向 `.invalid` 分支时，直接连边 **`verify_amount.invalid -> fail`**，主动触发子图回滚并向父节点抛出 `execution` 异常；
* 父图的 `settlement_transaction.tx_failed` 分支连接补偿节点 `enqueue_retry_job`，为 `status_note` 提供定值后走向 `end.deferred`。

---

## 完整工作流定义

```yaml
input:
  order_id: { type: string }
output:
  settlement_id: { type: string }
  status_note: { type: string }
branches:
  settled:
    description: "清算完成"
    output: [settlement_id, status_note]
  deferred:
    description: "外部依赖故障、超时或校验异常，已转入延迟队列"
    output: [status_note]

graph:
  nodes:
    # SESE 内联子图作为多步事务块：内部 3 个步骤共享总超时与回滚边界
    settlement_transaction:
      timeout_ms: 15000
      input: [order_id]
      output: [settlement_id, status_note]
      catch:
        tx_failed: [execution, timeout, contract]
      graph:
        nodes:
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

          verify_amount:
            input: [order_amount]
            branches:
              valid: "金额合法"
              invalid: "金额为负数或异常"
            run: "校验 variables.order_amount 是否大于零且在合理区间。"

          submit_clearing:
            timeout_ms: 5000
            input: [order_id, order_amount]
            output:
              settlement_id: { type: string }
              status_note: { type: string }
            run: "调用清算服务提交 variables.order_id 与 variables.order_amount，写入 output.settlement_id 与 output.status_note。"

        edges:
          - start -> fetch_order
          - fetch_order -> verify_amount
          # 主动触发子图栈帧回滚，向父节点抛出 execution 异常
          - verify_amount.invalid -> fail
          - verify_amount.valid -> submit_clearing
          - submit_clearing -> end

    # 异常补偿节点：在 .tx_failed 分支上定值 status_note
    enqueue_retry_job:
      input: [order_id]
      output:
        status_note: { type: string }
      run: "将订单 variables.order_id 加入延迟重试队列，并将排队说明写入 output.status_note。"

  edges:
    - start -> settlement_transaction
    - settlement_transaction -> end.settled
    - settlement_transaction.tx_failed -> enqueue_retry_job
    - enqueue_retry_job -> end.deferred
```
