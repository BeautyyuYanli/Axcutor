# 02. 异构结构化并发：Fork-Join 并发组（`-> [a, b] ->`）

当工作流需要在某个时刻同时启动多个职责不同、产出不同变量的**异构节点**（例如一边调用排版服务生成发布稿，一边运行合规检查生成审计报告），并在所有分支完成后汇合继续下游流程时，使用三元结构化并发边：**`source -> [worker_a, worker_b, ...] -> join_target`**。

---

## 一、 三元 Fork-Join 并发边语法

在 `graph.edges` 中，分叉（Fork）与汇合（Join）被绑定在同一条边声明里，保证所有并发分支必然在明确的屏障点汇合：

* **无分支起点触发**：`source -> [worker_a, worker_b] -> join_target`（其中 `source` 可以是普通无分支节点，也可以直接是图起点 `start`；`join_target` 可以是普通节点，也可以直接是 `end` 或 `end.<branch>`）。
* **条件分支触发**：`source.choice -> [worker_a, worker_b] -> join_target`。

---

## 二、 并发无竞争保障：只读快照与写集互斥

Axcutor 让异构并发组的执行结果具备 100% 的确定性，无需手动加锁：

1. **Fork 时刻只读快照（消除读-写竞争）**：
   * 在进入并发组的瞬间，运行时锁定当前图 `variables` 的只读快照。并发组内的各个分支读取到的都是这份 Fork 时刻的快照，绝不会因为兄弟分支执行快慢不同而读到对方中途修改的值。
2. **并发写集合互斥（消除写-写竞争）**：
   * 同一个并发组 `[worker_a, worker_b]` 中的各个节点，向所属图 `variables` 写回的目标变量名**必须两两互不重叠**（若两个并发分支试图写入同一个外层变量名，编译期直接报错）。
   * 只有当同组所有并发分支全部正常到达 Join 屏障时，各分支产出的变量才一次性原子合并回外层图的 `variables`。

---

## 三、 并发分支的单入口单出口（SESE）规则

出现在 `[worker_a, worker_b]` 中的节点不能再单独编写额外的入边或出边，且**直接成员节点不能对外暴露 `branches` 或 `catch`**。

> **如果某一个并发分支内部需要执行多步操作、条件分支或循环怎么办？**
> 只需将该并发分支写成一个**内联 `graph` 节点**（见 [03. Inline Graph](../01-foundations/03-inline-graphs.md)）！在子图内部可以自由使用多步串联、`branches`、成环回边或 `catch`，只要最终在子图内部汇合到子图唯一的 `-> end` 即可。

### 完整示例：在并发组中混编 `use`、`run` 与内联 `graph`

```yaml
input:
  raw_draft: { type: string }
output:
  formatted_doc: { type: string }
  compliance_report: { type: string }
  entity_summary: { type: string }

graph:
  nodes:
    # 并发分支 1：通过 use 调用外部排版工作流，写入 formatted_doc
    format_document:
      use: workflow:publishing/markdown_formatter
      with:
        input:
          content: raw_draft
          line_width: 100
        output:
          formatted_text: formatted_doc

    # 并发分支 2：直接通过 run 执行合规扫描，写入 compliance_report
    scan_compliance:
      input: [raw_draft]
      output:
        compliance_report: { type: string }
      run: "扫描文稿 variables.raw_draft 的合规性，生成审计报告写入 output.compliance_report。"

    # 并发分支 3：通过内联 graph 在单个并发分支内串联两步处理，写入 entity_summary
    extract_entities_pipeline:
      input: [raw_draft]
      output: [entity_summary]
      graph:
        nodes:
          ner_extract:
            input: [raw_draft]
            output:
              raw_entities:
                type: array
                items: { type: string }
            run: "从 variables.raw_draft 中抽取核心实体列表写入 output.raw_entities。"

          summarize_entities:
            input: [raw_entities]
            output:
              entity_summary: { type: string }
            run: "汇总实体列表 variables.raw_entities 生成术语表说明写入 output.entity_summary。"

        edges:
          - start -> ner_extract
          - ner_extract -> summarize_entities
          - summarize_entities -> end

  edges:
    # 三个并发分支共享 raw_draft 快照，分别产出 formatted_doc、compliance_report、entity_summary，在 end 处汇合
    - start -> [format_document, scan_compliance, extract_entities_pipeline] -> end
```

---

接下来我们将进入教程的第四部分，了解图栈帧与变量作用域规则、执行护栏以及异常处理机制：[01. 图栈帧与命名空间](../04-state-and-resilience/01-frames-and-scoping.md)。
