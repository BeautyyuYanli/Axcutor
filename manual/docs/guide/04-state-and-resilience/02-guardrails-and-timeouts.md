# 02. 执行护栏：`max_steps`、调用栈深度与 `timeout_ms`

在生产环境中运行包含动态循环（成环回边）、子工作流调用（`use`）与外部 I/O 的工作流时，Axcutor 提供三道自动护栏，确保工作流始终在受控的时间与资源边界内运行。

---

## 一、 单图执行步数上限（`graph.max_steps`）

每个激活的 `graph`（包括根工作流的 `graph`、`use` 调用的子工作流以及每次激活的内联 `graph`）都拥有自己独立的步数计数器，用于防止成环回边条件不收敛导致的无限循环。

```yaml
graph:
  max_steps: 50   # 可选，正整数；未显式声明时默认上限为 1000 步
  nodes: ...
  edges: ...
```

* **怎么计步？**
  * 每激活一个普通节点计 **`1` 步**；
  * 启动一个并发组 `-> [a, b, c] ->` 按组内激活的节点数计步（此处计 **`3` 步**）；
  * 挂载了 `for_each` 的节点按**实际遍历的元素个数**累加步数（例如遍历 10 个元素计 **`10` 步**）。
* **超出 `max_steps` 会发生什么？**
  * 如果发生在**内联 `graph` 或 `use` 子工作流**中，会立即中断并回滚该子图，向外层父节点抛出可恢复的 **`max_steps` 异常**——外层节点可以用 `catch` 捕获该异常并走降级分支！
  * 如果发生在**最顶层根图**自身，则直接终止整个工作流。

---

## 二、 调用栈深度上限（`max_call_depth`）

为了防止工作流直接递归（`A -> use A`）或相互递归（`A -> use B -> use A`）导致爆栈，运行时按调用链路径追踪栈深度（根图为 `1`，每进入一层 `use` 或内联 `graph` 深度 `+1`，退出时 `-1`；并发分支之间独立计算，互不横向累加）。

当任意调用路径深度超过引擎阈值（默认 **`64`**）时，立即触发不可捕获的 `StackOverflowError` 终止执行。

---

## 三、 节点级单次超时与原地快照重试（`timeout_ms` & `retry`）

在任何业务节点（`run`、内联 `graph`、`use: workflow:<path>`）上，都可以配置 `timeout_ms` 和 `retry`：

```yaml
nodes:
  call_payment_api:
    timeout_ms: 5000                  # 单次尝试（Per-Attempt）最大耗时：5000 毫秒
    retry:
      max_attempts: 3                 # 最大尝试总次数（含首次执行，>= 2）
      backoff_ms: 1000                # 两次尝试间的退避等待毫秒数（默认 0）
      on: [execution, timeout]        # 允许触发重试的异常类型（默认 [execution, timeout]）
    input: [payment_request]
    output:
      receipt_id: { type: string }
    run: "调用支付接口处理请求 variables.payment_request，将凭证号写入 output.receipt_id。"
```

### 关键行为特性：
1. **`timeout_ms` 针对每一次独立尝试**：如果设置了 `timeout_ms: 5000` 和 `max_attempts: 3`，那么每次重试都享有完整的 5 秒超时预算。
2. **基于只读输入快照的干净重放**：某次尝试失败或超时后，运行时会**彻底丢弃该次尝试产生的一切半成品状态**（如果是内联 `graph` 则销毁重建子栈帧），等待 `backoff_ms` 后用进入节点前锁定的同一份 `input` 只读快照重新执行。
3. **与 `for_each` 结合时的元素级重试**：当 `for_each` 节点配置了 `timeout_ms` / `retry` 时，仅对发生失败的**那单个元素**原地重试，同一批次里已经成功的其他元素不会重跑。

---

如果重试耗尽后依然失败，或者发生了无需重试的异常，我们该如何捕获并优雅恢复？请阅读本教程的最后一节：[03. 错误处理：异常捕获（`catch`）与主动回滚（`fail`）](./03-error-handling.md)。
