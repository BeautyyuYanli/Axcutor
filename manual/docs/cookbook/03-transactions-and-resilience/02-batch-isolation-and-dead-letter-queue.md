# 配方 02：`for_each` 批处理元素级超时重试、异常隔离与死信队列（DLQ）分流

## 场景与问题

在批量并发执行一组沙箱脚本任务 `raw_tasks` 时：
1. **单元素超时重试**：若某个任务执行超时（`> 5000ms`），仅对该任务原地重试 1 次（`max_attempts: 2`），不影响其他任务。
2. **元素级异常隔离与死信分流**：
   * 若重试后仍然超时（`timeout`），将该任务的原始结构体零拷贝归入 `timed_out_tasks` 队列；
   * 若脚本执行崩溃（`execution`），将该任务的原始结构体零拷贝归入 `crashed_tasks` 死信队列；
   * 所有正常完成的任务，将其标准输出 `stdout_text` 收集到 `completed_outputs` 列表中。
3. **契约损坏整批熔断**：若发生未在 `catch` 中列出的 `contract` 异常，则立即触发 Fail-Fast 整批取消。

---

## 完整工作流定义

```yaml
input:
  raw_tasks:
    type: array
    items:
      type: object
      properties:
        task_id: { type: string }
        script: { type: string }
      required: [task_id, script]
output:
  completed_outputs:
    type: array
    items: { type: string }
  timed_out_tasks:
    type: array
    items:
      type: object
      properties:
        task_id: { type: string }
        script: { type: string }
      required: [task_id, script]
  crashed_tasks:
    type: array
    items:
      type: object
      properties:
        task_id: { type: string }
        script: { type: string }
      required: [task_id, script]

graph:
  nodes:
    batch_execute_scripts:
      for_each:
        source: raw_tasks
        item: current_task
        concurrency: 4
        # 1. 按 catch 分支名，零拷贝将原始 current_task 结构体分流至对应的死信/重试队列
        filter:
          on_timeout: timed_out_tasks
          on_crash: crashed_tasks
        # 2. 自动仅从正常完成的迭代中收集 stdout_text（异常迭代等价于 output: [] 自动跳过）
        collect:
          stdout_text: completed_outputs
      timeout_ms: 5000
      retry:
        max_attempts: 2
        on: [timeout]                 # 仅对单元素超时原地重试
      catch:
        on_timeout: [timeout]         # 重试后仍超时 -> 走 on_timeout 分支归入 timed_out_tasks
        on_crash: [execution]         # 沙箱脚本报错 -> 走 on_crash 分支归入 crashed_tasks
      input: [current_task]
      output:
        stdout_text: { type: string }
      run: "在沙箱中执行任务 variables.current_task.task_id 的脚本 variables.current_task.script，将标准输出写入 output.stdout_text。"

  edges:
    - start -> batch_execute_scripts
    - batch_execute_scripts -> end
```
