# 配方 02：`for_each` 批处理元素级超时重试、异常隔离与死信队列（DLQ）分流

## 一、 场景与需求

批量并发执行沙箱脚本任务列表 `raw_tasks` 时，需按故障性质实现元素级容错隔离与分级分流：

1. **单元素超时原地重试**：单个任务单次尝试限时 `5000ms`；若执行超时，仅对该任务原地重试 1 次，同批次其他任务不受影响。
2. **元素级异常隔离与死信分流**：单个任务的超时或运行期崩溃不中断整批任务，而是按单元素最终结局分流归集：
   * 正常完成的任务，收集其标准输出至 `completed_outputs` 列表；
   * 重试耗尽仍超时的任务，保留其原始任务结构体并归入 `timed_out_tasks` 列表；
   * 运行期报错崩溃的任务，保留其原始任务结构体并归入 `crashed_tasks` 死信列表。
3. **契约违约整批快速失败（Fail-Fast）**：若任意任务发生输入/输出结构不合法的契约违约，立即取消同批在途任务并向上冒泡终止整批流程。

---

## 二、 核心原语组合要点

将 `for_each` 挂载在 SESE 内联 `graph` 节点上，把单次元素迭代的超时重试、异常捕获与死信分流封装在子图内部：

* **子图内重试与选择性捕获（`timeout_ms` + `retry` + `catch`）**：在子图动作节点 `run_sandbox` 上声明 `timeout_ms: 5000` 与 `retry: { max_attempts: 2, on: [timeout] }` 实现单元素原地重试；通过 `catch` 仅将 `timeout` 与 `execution` 异常分别物化为 `on_timeout` 与 `on_crash` 分支，未列出的 `contract` 异常直接跨子图向上冒泡以触发整批 Fail-Fast 取消。
* **死信负载零拷贝透传（[`builtin:assign`](../../reference/builtins/assign.md)）**：在 `on_timeout` 与 `on_crash` 分支下分别串联 `builtin:assign`，将当前迭代项 `current_task` 零拷贝赋值给 `timed_out_tasks` 与 `crashed_tasks`，并分别走向子图分支终点 `end.timed_out` 与 `end.crashed`。
* **外层按分支子集自动归集（`for_each` + `branches`）**：外层 `for_each` 根据 `branches.<branch>.output` 声明的产出子集，自动将落入 `ok`、`timed_out`、`crashed` 分支的单次迭代产出分别聚合至 `completed_outputs`、`timed_out_tasks` 与 `crashed_tasks` 列表，并在全批结束后收敛为单条无分支出边。

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
    # 并发批处理节点：在单次元素迭代内执行超时计时、重试与异常零拷贝死信分流
    batch_execute_scripts:
      for_each:
        source: raw_tasks
        item: current_task
        concurrency: 4
      output:
        completed_outputs: { type: string }
        timed_out_tasks:
          type: object
          properties:
            task_id: { type: string }
            script: { type: string }
          required: [task_id, script]
        crashed_tasks:
          type: object
          properties:
            task_id: { type: string }
            script: { type: string }
          required: [task_id, script]
      branches:
        ok:
          description: "脚本正常执行完毕"
          output: [completed_outputs]
        timed_out:
          description: "单元素重试耗尽仍超时，归档原始任务至 timed_out_tasks"
          output: [timed_out_tasks]
        crashed:
          description: "脚本运行崩溃，归档原始任务至 crashed_tasks"
          output: [crashed_tasks]
      graph:
        nodes:
          # 子图步骤 1：执行沙箱脚本，配置单次尝试 5000ms 超时、对 timeout 原地重试 2 次，并捕获 timeout 与 execution
          # 未在 catch 中列出的 contract 异常直接向父图冒泡触发整批 Fail-Fast 取消
          run_sandbox:
            timeout_ms: 5000
            retry:
              max_attempts: 2
              on: [timeout]
            catch:
              on_timeout: [timeout]
              on_crash: [execution]
            input: [current_task]
            output:
              completed_outputs: { type: string }
            run: "在沙箱中执行任务 variables.current_task.task_id 的脚本 variables.current_task.script，将标准输出写入 output.completed_outputs。"

          # 子图步骤 2a：在 on_timeout 分支上，零拷贝将原始 current_task 赋值给 timed_out_tasks
          record_timed_out:
            use: builtin:assign
            with:
              input:
                source: current_task
              output:
                value: timed_out_tasks

          # 子图步骤 2b：在 on_crash 分支上，零拷贝将原始 current_task 赋值给 crashed_tasks
          record_crashed:
            use: builtin:assign
            with:
              input:
                source: current_task
              output:
                value: crashed_tasks

        edges:
          - start -> run_sandbox
          - run_sandbox -> end.ok
          - run_sandbox.on_timeout -> record_timed_out
          - record_timed_out -> end.timed_out
          - run_sandbox.on_crash -> record_crashed
          - record_crashed -> end.crashed

  # 3. 外层控制流边：for_each 内部已消费 ok / timed_out / crashed 分支，对外为单条无分支出边
  edges:
    - start -> batch_execute_scripts
    - batch_execute_scripts -> end
```
