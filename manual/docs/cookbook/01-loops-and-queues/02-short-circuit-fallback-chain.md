# 配方 02：候选节点短路早退遍历与失败轨迹记录（`array_shift` + `array_append`）

## 一、 场景与需求

给定按优先级排列的候选服务地址列表 `candidate_endpoints` 与请求报文 `request_payload`，需满足以下编排行为：

1. **按序逐个尝试与命中即停（Short-Circuit）**：按列表顺序依次向候选节点发起请求；一旦某个节点成功返回响应，立即结束工作流并返回结果，不再访问队列中剩余的候选节点（因此不使用全量遍历的 `for_each`）。
2. **失败轨迹累积**：若当前节点调用失败，将该节点的失败摘要追加至 `failed_trace` 列表，继续取出下一个节点尝试，直至某个节点成功或候选队列耗尽。

---

## 二、 核心原语组合要点

1. **队列首元素拆分与重绑定（[`builtin:array_shift`](../../reference/builtins/array-shift.md)）**：遵循纯函数式写时复制（Copy-on-Write）不可变语义，从 `candidate_endpoints` 头部取出首元素绑定至 `current_endpoint`（在 `graph.variables` 中声明其 `TypeSpec`），同时将剩余切片 `rest` 构造为剩余元素的新列表副本并重新绑定至同名变量 `candidate_endpoints`。队列非空时走 `.some` 分支，队列耗尽时走 `.empty` 分支。
2. **失败记录追加（[`builtin:array_append`](../../reference/builtins/array-append.md)）**：在 `try_call.failed` 分支下，将单步产出的 `error_entry` 追加至 `failed_trace` 列表末尾（生成新列表副本并重新绑定至 `failed_trace`），随后沿回边跳回 `pop_endpoint`。

---

## 三、 完整工作流定义

```yaml
# 1. 根节点输入契约
input:
  candidate_endpoints:
    type: array
    items: { type: string }
  request_payload:
    type: string

# 2. 根节点输出契约
output:
  response_data:
    type: string
  failed_trace:
    type: array
    items: { type: string }

# 3. 根节点排他分支契约：成功时返回响应与前置失败轨迹，全败时仅返回失败轨迹
branches:
  ok:
    description: "命中可用节点并获取响应"
    output: [response_data, failed_trace]
  all_failed:
    description: "所有候选节点均尝试失败"
    output: [failed_trace]

graph:
  max_steps: 50

  # 4. 声明带初值的失败轨迹列表与未初始化的当前节点变量 current_endpoint
  variables:
    failed_trace:
      type: array
      items: { type: string }
      default: []
    current_endpoint:
      type: string

  nodes:
    # 步骤 1：取出首元素 current_endpoint，构造剩余元素的新列表副本并重新绑定至同名变量 candidate_endpoints
    pop_endpoint:
      use: builtin:array_shift
      with:
        input:
          source: candidate_endpoints
        output:
          item: current_endpoint
          rest: candidate_endpoints

    # 步骤 2：调用当前取出的候选节点
    try_call:
      input: [current_endpoint, request_payload]
      output:
        response_data: { type: string }
        error_entry: { type: string }
      branches:
        success:
          description: "当前节点调用成功，短路退出"
          output: [response_data]
        failed:
          description: "当前节点调用失败，产出单条错误摘要"
          output: [error_entry]
      run:
        executor: builtin
        content: |
          if input.current_endpoint.startswith("https://"):
              return Branch.success(
                  response_data=f"OK from {input.current_endpoint}: {input.request_payload}"
              )
          return Branch.failed(
              error_entry=f"{input.current_endpoint}: connection refused"
          )

    # 步骤 3：将单次失败摘要 error_entry 追加至 failed_trace 列表末尾并重新绑定至同名变量
    record_failure:
      use: builtin:array_append
      with:
        input:
          source: failed_trace
          item: error_entry
        output:
          result: failed_trace

  # 5. 控制流连边：组合 .some / .empty 分支、短路出口与成环回边
  edges:
    - start -> pop_endpoint
    # 队列非空（.some）：current_endpoint 已定值，发起调用尝试
    - pop_endpoint.some -> try_call
    # 命中即停（短路早退）：直接跳出循环至 end.ok，不再消费剩余候选节点
    - try_call.success -> end.ok
    # 当前节点失败：追加错误记录后沿回边跳回 pop_endpoint 继续取出下一项
    - try_call.failed -> record_failure
    - record_failure -> pop_endpoint
    # 队列已空（.empty）：所有候选节点均已耗尽，进入 end.all_failed
    - pop_endpoint.empty -> end.all_failed
```
