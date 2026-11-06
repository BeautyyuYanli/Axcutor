# 配方 02：候选节点短路早退遍历与失败轨迹记录（`array_shift` + `array_append`）

## 场景与问题

给定一个按优先级排列的候选服务地址列表 `candidate_endpoints`：
* 我们需要按顺序依次尝试向候选节点发起请求；
* **一旦某个节点成功返回响应，立即短路早退结束工作流**，不再请求队列中剩余的节点（因此不适合使用全量遍历的 `for_each`）；
* 若某个节点请求失败，将该节点的失败摘要追加到 `failed_trace` 列表中，继续弹出下一个节点尝试，直到成功或队列耗尽。

## 解决方案要点

* 使用 [`builtin:array_shift`](../../reference/builtins/array-shift.md) 从列表头部零开销弹出首元素 `current_endpoint`，并将剩余切片 `rest` 原地写回 `candidate_endpoints`；
* 当 `pop_endpoint.some` 时发起尝试：若成功（`try_call.success`）直接走向 `end.ok` 短路退出；
* 若失败（`try_call.failed`），使用 [`builtin:array_append`](../../reference/builtins/array-append.md) 将错误记录追加到 `failed_trace` 末尾，再沿成环回边跳回 `pop_endpoint`。

---

## 完整工作流定义

```yaml
input:
  candidate_endpoints:
    type: array
    items: { type: string }
  request_payload:
    type: string
output:
  response_data:
    type: string
  failed_trace:
    type: array
    items: { type: string }
branches:
  ok:
    description: "命中可用节点并成功获取响应"
    output: [response_data, failed_trace]
  all_failed:
    description: "所有候选节点均尝试失败"
    output: [failed_trace]

graph:
  max_steps: 50
  variables:
    # 显式声明带类型的空数组初值，供 builtin:array_append 累积失败轨迹
    failed_trace:
      type: array
      items: { type: string }
      default: []

  nodes:
    # 1. 从队列头部弹出首元素 current_endpoint，剩余切片原地覆盖回 candidate_endpoints
    pop_endpoint:
      use: builtin:array_shift
      with:
        input:
          source: candidate_endpoints
        output:
          item: current_endpoint
          rest: candidate_endpoints

    # 2. 尝试调用当前弹出的候选节点
    try_call:
      input: [current_endpoint, request_payload]
      output:
        response_data: { type: string }
        error_entry: { type: string }
      branches:
        success:
          description: "当前节点调用成功，立即早退"
          output: [response_data]
        failed:
          description: "当前节点调用失败，产出错误记录"
          output: [error_entry]
      run: "向节点 variables.current_endpoint 发送请求 variables.request_payload；成功则写入 output.response_data 并选 success；失败则将节点名与错误原因写入 output.error_entry 并选 failed。"

    # 3. 将单次失败记录追加到 failed_trace 末尾
    record_failure:
      use: builtin:array_append
      with:
        input:
          source: failed_trace
          item: error_entry
        output:
          result: failed_trace

  edges:
    - start -> pop_endpoint
    # 队列非空（.some）：current_endpoint 已定值，发起尝试
    - pop_endpoint.some -> try_call
    # 命中即停（短路早退）：直接跳出循环至 end.ok，不再消费剩余节点
    - try_call.success -> end.ok
    # 当前节点失败：记录日志后沿回边跳回 pop_endpoint 继续取下一个
    - try_call.failed -> record_failure
    - record_failure -> pop_endpoint
    # 队列已耗尽（.empty）：进入 end.all_failed
    - pop_endpoint.empty -> end.all_failed
```
