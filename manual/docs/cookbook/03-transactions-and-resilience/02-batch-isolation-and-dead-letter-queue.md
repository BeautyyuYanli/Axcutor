# 配方 02：`for_each` 批处理元素级超时重试、异常隔离与死信队列（DLQ）分流

## 一、 场景与需求

批量并发执行沙箱脚本任务列表 `raw_tasks` 时，需实现元素级容错与分流：

1. **单元素超时原地重试**：单个任务单次尝试超时上限为 `5000ms`；若发生超时（`timeout`），仅对该任务原地重试 1 次（`max_attempts: 2`），同批次其他任务不受影响。
2. **元素级异常隔离与死信分流**：
   * 重试后仍超时的任务（`timeout`），将其原始结构体零拷贝归入 `timed_out_tasks` 列表；
   * 执行期报错崩溃的任务（`execution`），将其原始结构体零拷贝归入 `crashed_tasks` 死信列表；
   * 正常完成的任务，将其标准输出 `stdout_text` 收集至 `completed_outputs` 列表。
3. **契约违约整批快速失败（Fail-Fast）**：若发生未在 `catch` 中列出的 `contract` 异常，立即取消同批在途任务并向上冒泡。

---

## 二、 核心原语组合要点

在 `for_each` 节点上同时组合 `timeout_ms`、`retry`、`catch`、`filter` 与 `collect`：

* `catch` 将单次元素迭代的 `timeout` 与 `execution` 异常分别物化为迭代级分支 `on_timeout` 与 `on_crash`（异常分支等价于 `output: []`）。
* `for_each.filter` 按 `on_timeout` 与 `on_crash` 分支名零拷贝保留原始任务结构体。
* `for_each.collect` 自动跳过未产出 `stdout_text` 的异常迭代，仅收集正常完成迭代的输出：

```yaml
batch_execute_scripts:
  for_each:
    source: raw_tasks
    item: current_task
    concurrency: 4
    filter:
      on_timeout: timed_out_tasks     # 捕获 timeout 异常的原始元素归入 timed_out_tasks
      on_crash: crashed_tasks         # 捕获 execution 异常的原始元素归入 crashed_tasks
    collect:
      stdout_text: completed_outputs  # 仅收集正常完成迭代的 stdout_text
  timeout_ms: 5000
  retry:
    max_attempts: 2
    on: [timeout]
  catch:
    on_timeout: [timeout]
    on_crash: [execution]
```

---

## 三、 完整工作流定义

```yaml
# 1. 根节点输入契约
input:
  raw_tasks:
    type: array
    items:
      type: object
      properties:
        task_id: { type: string }
        script: { type: string }
      required: [task_id, script]

# 2. 根节点输出契约：包含正常输出列表与两个分类死信队列列表
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
    # 并发批处理节点：在单次元素迭代粒度上执行超时计时、重试与异常分流
    batch_execute_scripts:
      for_each:
        source: raw_tasks
        item: current_task
        concurrency: 4
        # 1. 按 catch 异常分支名，零拷贝将原始 current_task 结构体分流至对应队列
        filter:
          on_timeout: timed_out_tasks
          on_crash: crashed_tasks
        # 2. 仅从正常完成的迭代中收集 stdout_text（进入 catch 的迭代等价于 output: []，自动跳过）
        collect:
          stdout_text: completed_outputs
      # 单元素单次尝试超时上限 5000ms
      timeout_ms: 5000
      # 仅对单元素 timeout 异常原地重试（含首次共最多 2 次）
      retry:
        max_attempts: 2
        on: [timeout]
      # 拦截重试后仍未恢复的 timeout 及脚本运行期 execution 异常；未列出的 contract 异常直接冒泡
      catch:
        on_timeout: [timeout]
        on_crash: [execution]
      input: [current_task]
      output:
        stdout_text: { type: string }
      run: "在沙箱中执行任务 variables.current_task.task_id 的脚本 variables.current_task.script，将标准输出写入 output.stdout_text。"

  # 3. 外层控制流边：for_each 内部已消费 catch 分支，对外为单条无分支出边
  edges:
    - start -> batch_execute_scripts
    - batch_execute_scripts -> end
```
