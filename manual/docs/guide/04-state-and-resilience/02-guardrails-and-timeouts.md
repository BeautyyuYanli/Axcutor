# 02. 执行护栏：`max_steps`、调用栈深度与 `timeout_ms`

在运行包含动态循环（成环回边）、跨包子工作流调用（`use`）与外部 I/O 的工作流时，Axcutor 提供单图步数上限、调用栈深度上限与节点级超时重试三项执行护栏，控制工作流的资源消耗与执行时长。

---

## 一、 单图执行步数上限（`graph.max_steps`）

每个激活的 `graph`（包括根工作流的 `graph`、`use` 调用的子工作流以及每次激活的内联 `graph`）均维护独立的步数计数器，用于限制成环回边的最大迭代规模。

### 1. 计步规则

| 调度的图内元素 | 消耗步数 |
| :--- | :--- |
| 普通单次节点（`run`、`use: workflow:<PackagePath>`、`use: builtin:<op>` 或内联 `graph` 本身） | 每次激活计 **`1` 步** |
| Fork-Join 并发组（`-> [a, b, c] ->`） | 按组内并发激活的节点数量计步（例如 3 个分支计 **`3` 步**） |
| 挂载 `for_each` 的节点 | 按实际启动的元素迭代个数逐个累加步数（例如遍历 10 个元素计 **`10` 步**；空集合计 `0` 步） |

* **与节点原地重试（`retry`）的关系**：节点内部的 `retry` 原地重试属于同一次节点调度（或单个 `for_each` 元素的同一次迭代），**不会额外增加**所属图的 `max_steps` 步数计数。
* **与子图 / 子工作流的关系**：内联 `graph` 或 `use: workflow:<PackagePath>` 子工作流在父图中仅按一次节点激活计 **`1` 步**；其内部调度的节点步数在子栈帧中独立计量，受该子图自身的 `max_steps` 约束。

### 2. 超限并发协同取消、回滚与异常捕获

当图栈帧在调度普通节点、推进并发组或启动 `for_each` 新元素迭代期间，检测到累计步数超过 `max_steps` 时，运行时按以下步骤执行清理与异常传播：

1. **协同取消在途并发任务**：立即停止派发新任务，向当前图栈帧树下所有尚未完成的并发分支、`for_each` 迭代及子栈帧广播不可捕获的 `CancelledError`（触发底层执行器 `abort_signal`），并等待全部在途任务退出。
2. **销毁当前图栈帧并回滚未提交修改**：待所有在途任务退出后，销毁当前图栈帧并丢弃该图未提交的所有中间变量修改。
3. **按栈帧层级抛出异常或终止工作流**：
   * **在内联 `graph` 或 `use` 子工作流内超限**：向外层调用节点抛出可恢复的 **`max_steps` 异常**；外层节点可通过 `catch` 拦截该异常并转入降级分支。
   * **在顶层根图自身超限**：直接抛出不可恢复的 `MaxStepsExceededError`，终止整个工作流。

```yaml
nodes:
  # 内联子图设置独立的 max_steps: 10；若内部循环超过 10 步，外层通过 catch 捕获 max_steps 异常
  bounded_loop_subflow:
    input: [initial_prompt]
    output:
      refined_text: { type: string }
    catch:
      step_limit_reached: [max_steps]
    graph:
      max_steps: 10   # 仅统计该内联子图内部调度的节点步数（未配置时默认 1000）
      nodes: ...
      edges: ...

edges:
  - start -> bounded_loop_subflow
  - bounded_loop_subflow -> end
  # 子图步数超限时回滚子图修改，转入兜底处理节点
  - bounded_loop_subflow.step_limit_reached -> use_fallback_text
  - use_fallback_text -> end
```

---

## 二、 调用栈深度上限（`max_call_depth`）

为防止工作流直接递归（`A -> use A`）或相互递归（`A -> use B -> use A`）导致调用栈溢出，运行时按调用链路径追踪栈深度：

* **深度增减规则**：根图深度为 `1`；每进入一层 `use: workflow:<PackagePath>`（`Function Frame`）或内联 `graph`（`Block Frame`），当前路径调用深度 `+1`，退出时 `-1`。
* **并发分支独立计算**：Fork-Join 并发分支或 `for_each` 并发迭代各自沿其父节点深度独立向下累加，兄弟分支之间不横向叠加。
* **超限行为**：当任意调用路径深度超过引擎上限（默认 **`64`**）时，立即触发不可捕获的致命错误 `StackOverflowError` 并终止工作流。

---

## 三、 节点级单次超时与原地快照重试（`timeout_ms` & `retry`）

在任何业务节点（`run`、内联 `graph`、`use: workflow:<PackagePath>`）上均可声明 `timeout_ms` 与 `retry`（注：零开销确定性算子 `use: builtin:*` 不支持 `timeout_ms` 与 `retry`）。

### 1. 单节点超时与重试配置

* **`timeout_ms`**：作用于**每一次独立尝试（Per-Attempt）**。若配置 `timeout_ms: 5000` 与 `max_attempts: 3`，则每次尝试均享有独立的 5000 毫秒超时预算。
* **`retry` 快照重放**：当单次尝试因匹配 `retry.on` 的异常失败时，运行时丢弃该次尝试产生的一切未提交状态，等待 `backoff_ms` 后使用进入节点前锁定的同一份 `input` 只读快照重新执行。

```yaml
nodes:
  call_payment_api:
    timeout_ms: 5000                  # 单次尝试最大耗时：5000 毫秒
    retry:
      max_attempts: 3                 # 最大尝试总次数（含首次执行，必须 >= 2）
      backoff_ms: 1000                # 两次尝试间的退避等待毫秒数（默认 0）
      on: [execution, timeout]        # 触发重试的异常类型列表（默认 [execution, timeout]）
    input: [payment_request]
    output:
      receipt_id: { type: string }
    run:
      executor: builtin
      content: |
        return Output(receipt_id=f"PAY-{input.payment_request}")
```

### 2. 与 `for_each` 结合时的元素级隔离重试

当挂载了 `for_each` 的节点同时配置 `timeout_ms` 与 `retry` 时，超时计时器与重试计数器**绑定在单个元素迭代（Per-Item Attempt）上**：仅对发生失败的单个元素执行原地重试，同一批次中已成功完成的其他元素不会重复执行：

```yaml
nodes:
  batch_enrich_users:
    for_each:
      source: user_ids
      item: current_uid
      concurrency: 5
    # 每个 current_uid 的单次尝试超时为 3000ms；某元素超时仅重跑该元素（最多 3 次）
    timeout_ms: 3000
    retry:
      max_attempts: 3
      backoff_ms: 500
      on: [execution, timeout]
    input: [current_uid]
    # 单次迭代产出 string，在外层图中对应为已声明的 Array<string>
    output:
      enriched_profiles: { type: string }
    run:
      executor: builtin
      content: |
        return Output(enriched_profiles=f"profile:{input.current_uid}")
```

---

## 四、 完整示例：组合步数护栏、超时控制与快照重试

以下完整工作流在内联子图上配置 `max_steps` 与整体 `timeout_ms`，并在子图内部的外部接口调用节点上配置单次尝试超时与 `retry`：

```yaml
# 1. 根节点输入与输出契约
input:
  query_topic: { type: string }
output:
  final_summary: { type: string }

graph:
  nodes:
    # 阶段 1：带步数上限与总超时保护的内联探索子图
    # - timeout_ms: 30000 限制整个子图单次运行总耗时不超过 30 秒
    # - catch 拦截子图步数超限 (max_steps) 或总超时 (timeout)，转入降级节点
    bounded_research_stage:
      timeout_ms: 30000
      catch:
        degraded: [max_steps, timeout]
      input: [query_topic]
      output:
        final_summary: { type: string }
      graph:
        # 限制子图内部环路最多执行 12 步
        max_steps: 12
        variables:
          draft_notes:
            type: string
            default: ""
        nodes:
          # 子图步骤 1.1：调用外部检索服务，配置单次请求 4 秒超时与最多 3 次原地重试
          fetch_next_evidence:
            timeout_ms: 4000
            retry:
              max_attempts: 3
              backoff_ms: 1000
              on: [execution, timeout]
            input: [query_topic, draft_notes]
            output:
              draft_notes: { type: string }
            run:
              executor: builtin
              content: |
                updated = f"{input.draft_notes}\nEvidence for {input.query_topic}".strip()
                return Output(draft_notes=updated)

          # 子图步骤 1.2：评估资料是否充足，决定继续回边检索或产出最终摘要
          evaluate_sufficiency:
            input: [draft_notes]
            output:
              final_summary: { type: string }
            branches:
              enough:
                description: "资料充足，生成摘要并结束子图"
                output: [final_summary]
              need_more:
                description: "资料不足，继续下一轮检索"
                output: []
            run:
              executor: builtin
              content: |
                if len(input.draft_notes.splitlines()) >= 2:
                    return Branch.enough(final_summary=input.draft_notes)
                return Branch.need_more()

        edges:
          - start -> fetch_next_evidence
          - fetch_next_evidence -> evaluate_sufficiency
          - evaluate_sufficiency.need_more -> fetch_next_evidence
          - evaluate_sufficiency.enough -> end

    # 阶段 2（降级补偿节点）：当子图触发 max_steps 或 timeout 时，生成兜底摘要
    build_fallback_summary:
      input: [query_topic]
      output:
        final_summary: { type: string }
      run:
        executor: builtin
        content: |
          return Output(final_summary=f"Fallback summary for {input.query_topic}")

  # 2. 主图控制流连边
  edges:
    - start -> bounded_research_stage
    - bounded_research_stage -> end
    - bounded_research_stage.degraded -> build_fallback_summary
    - build_fallback_summary -> end
```

---

下一节：[03. 错误处理：异常捕获（`catch`）与主动回滚（`fail`）](./03-error-handling.md)。
