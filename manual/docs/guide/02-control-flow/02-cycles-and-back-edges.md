# 02. 成环回边：动态条件循环（While Loop）

当业务流程需要**事先未知迭代次数的动态条件收敛循环**（例如：生成代码 → 沙箱测试 → 失败则带着报错信息回退重生成，或者从待处理队列中逐个弹出候选节点尝试、一旦成功即刻停止）时，Axcutor 不需要额外的 `while` 语法块——只需在 `graph.edges` 中将分支出边指向上游节点，即构成**成环回边（Back-Edge）**。

---

## 一、 编写成环回边的三个要点

1. **在 `graph.variables` 中初始化跨轮次状态变量**：
   * 循环体入口节点在**第 1 轮**从 `start` 进入时，循环体下游节点尚未执行。因此，任何在循环体入口被读取、并由循环体内部更新的跨轮状态变量（如重试计数 `attempts`、上轮反馈 `last_error`），都必须在进入循环前已赋初值（通常在 `graph.variables` 中声明）。
2. **必须存在通往 `end` 的出口分支**：
   * 处于环路中的节点在拓扑上必须至少有一条分支路径能够跳出环路到达 `end`（或 `end.<branch>`），否则静态检查器会在编译期直接判定为死循环并报错。
3. **配合 `max_steps` 设置循环安全上限**：
   * 在包含环路的 `graph` 上可配置 `max_steps`（默认 `1000`），防止运行期条件长期不收敛。

### 基础示例：重试循环拓扑

```yaml
graph:
  max_steps: 20
  variables:
    attempts: 0
    last_error: ""

  nodes:
    generate_candidate:
      input: [task_spec, last_error, attempts]
      output:
        candidate: { type: string }
        attempts: { type: integer }
      run: "根据需求 variables.task_spec 与上次报错 variables.last_error 生成结果写入 output.candidate，并将 variables.attempts + 1 写入 output.attempts。"

    verify_candidate:
      input: [candidate, attempts]
      output:
        last_error: { type: string }
      branches:
        pass:
          description: "校验通过"
          output: []
        retry:
          description: "校验未过且未达上限，回边重試"
          output: [last_error]
      run: "检验 variables.candidate；通过选 pass，否则记录原因到 output.last_error 并选 retry。"

  edges:
    - start -> generate_candidate
    - generate_candidate -> verify_candidate
    - verify_candidate.retry -> generate_candidate  # 成环回边：跳回上游节点进入下一轮
    - verify_candidate.pass -> end
```

> 完整的生产级“生成-沙箱校验-修正”与早退失败分支示例，请参见 Cookbook：[生成-校验-修正闭环](../../cookbook/01-loops-and-queues/01-generate-verify-refine-loop.md)。

---

## 二、 循环内的零开销集合操作算子

在成环回边循环中，如果我们需要**从队列头部逐个弹出元素尝试（支持命中即停的短路早退遍历）**，或者在每轮循环中**向历史列表追加记录 / 更新字典键值**，可以直接调用三个零开销确定性集合算子（详见 [内建节点字典](../../reference/builtins/README.md)）：

| 内建算子 | 核心用途 | 控制分支与输出 |
| :--- | :--- | :--- |
| **[`builtin:array_shift`](../../reference/builtins/array-shift.md)** | 从列表头部弹出首元素 `item` 与剩余切片 `rest`（推荐将 `rest` 与源列表同名绑定，原地推进队列） | • **`.some`**：队列非空，`item` 与 `rest` 获得定值<br>• **`.empty`**：队列已空，跳出循环 |
| **[`builtin:array_append`](../../reference/builtins/array-append.md)** | 将单步产出的元素 `item` 追加到列表 `source` 末尾 | 无分支单出边，输出更新后的列表 `result` |
| **[`builtin:map_set`](../../reference/builtins/map-set.md)** | 将键值对 `key -> value` 插入或更新到有序映射 `source` 中 | 无分支单出边，输出更新后的有序映射 `result` |

> 使用 `builtin:array_shift` + `builtin:array_append` 实现“候选服务节点短路早退回退链”的完整代码模板，请参见 Cookbook：[候选节点短路早退遍历与失败轨迹记录](../../cookbook/01-loops-and-queues/02-short-circuit-fallback-chain.md)。

---

成环回边适用于未知迭代次数或需要中途短路早退的循环。当我们需要对已知集合的全部元素进行高并发处理、分流或汇总时，则使用结构化并发原语 `for_each`。请进入下一部分：[01. 同构结构化并发：集合遍历（`for_each`）](../03-concurrency/01-for-each.md)。
