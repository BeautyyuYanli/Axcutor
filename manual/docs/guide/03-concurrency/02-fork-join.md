# 02. 异构结构化并发：Fork-Join 并发组（`-> [a, b] ->`）

当工作流需要在同一时刻并发启动多个职责不同、产出不同变量的**异构节点**（如同时执行排版转换、合规扫描与实体抽取），并在所有并发分支完成后汇合继续下游流程时，使用三元结构化并发边：**`source -> [worker_a, worker_b, ...] -> join_target`**。

---

## 一、 三元 Fork-Join 并发边语法

在 `graph.edges` 中，分叉（Fork）与汇合（Join）声明在同一条三元边中，确保所有并发分支在显式屏障点（Join Barrier）完成同步：

1. **无分支源触发**：`source -> [worker_a, worker_b] -> join_target`（`source` 可为 `start`、普通无分支节点或挂载了 `for_each` 的节点；`join_target` 可为普通节点、`end` 或 `end.<branch>`）。
2. **条件分支源触发**：`source.choice -> [worker_a, worker_b] -> join_target`。

```yaml
edges:
  # 1. 从图起点 start 直接触发两个并发节点，全部完成后在 merge_report 汇合
  - start -> [fetch_profile, fetch_orders] -> merge_report

  # 2. 当 check_tier 节点走向 .enterprise 分支时触发三个并发节点，完成后直接汇合至 end.done
  - check_tier.enterprise -> [audit_security, allocate_quota, notify_sales] -> end.done
```

---

## 二、 并发隔离：Fork 快照锁定与写集合互斥

运行时通过两项机制消除并发组内的数据竞争：

### 1. Fork 时刻只读快照（消除读-写竞争）

进入并发组时，运行时锁定当前图 `variables` 的只读快照。组内所有并发分支读取的均是该 Fork 时刻的输入快照，任一分支的执行进度不会影响兄弟分支读取到的输入值。

### 2. 并发写集合两两互斥（消除写-写竞争）

同一并发组 `[worker_a, worker_b]` 内的各个分支节点，向所属图 `variables` 写回的目标变量集合必须**两两互不重叠（Disjoint Writes）**。若两个并发分支声明写入同一个外层变量名，编译期静态检查器将直接报错。当组内所有分支均正常到达 Join 屏障后，各分支产出一次性原子合并回外层图 `variables`。

```yaml
nodes:
  # 合法：worker_a 写入 report_a，worker_b 写入 report_b，写集合互不重叠
  worker_a:
    input: [raw_draft]
    output:
      report_a: { type: string }
    run: "..."

  worker_b:
    input: [raw_draft]
    output:
      report_b: { type: string }
    run: "..."

  # 非法（编译期报错）：若将 worker_c 与 worker_a 放入同一并发组，因两者均写入 report_a 违反写集合互斥
  worker_c:
    input: [raw_draft]
    output:
      report_a: { type: string }
    run: "..."
```

---

## 三、 并发分支的单入口单出口（SESE）约束

出现在并发组 `[worker_a, worker_b]` 中的成员节点必须满足单入口单出口（SESE）约束：

1. **独占拓扑（禁止额外连边或首尾自环）**：并发组成员节点既不得作为本条并发边自身的 `source` 或 `join_target`，也不得在 `graph.edges` 的其他任何边中作为源节点、目标节点、汇合点或再次出现在其他并发组中。
2. **禁止直接暴露未消费分支**：并发组成员节点（若未挂载 `for_each`）不得在自身节点层级直接声明 `branches` 或 `catch`。
3. **通过内联 `graph` 封装多步或分支逻辑**：若单个并发分支内部需要执行多步串联、条件分支、成环循环或异常恢复，将该并发分支声明为内联 `graph` 节点（见 [03. Inline Graph](../01-foundations/03-inline-graphs.md)），在子图内部汇合至子图自身的唯一出口 `-> end`：

```yaml
nodes:
  # 将包含条件分支与异常恢复的并发分支封装为 SESE 内联子图
  branch_with_internal_routing:
    input: [raw_draft]
    output: [entity_summary]
    graph:
      nodes:
        try_fast_extract:
          input: [raw_draft]
          output:
            entity_summary: { type: string }
          catch:
            fallback: "*"
          run: "快速抽取实体摘要写入 output.entity_summary。"

        fallback_extract:
          input: [raw_draft]
          output:
            entity_summary: { type: string }
          run: "使用兜底规则抽取实体摘要写入 output.entity_summary。"
      edges:
        - start -> try_fast_extract
        # 子图内部消费 catch 分支，最终统一收敛到子图唯一的 end
        - try_fast_extract -> end
        - try_fast_extract.fallback -> fallback_extract
        - fallback_extract -> end
```

---

## 四、 完整示例：在并发组中混编 `use`、`run` 与内联 `graph`

以下完整工作流在 `start` 处并发启动三个异构节点（分别采用 `use`、`run` 与内联 `graph` 形态），共享同一份 `raw_draft` 输入快照，各自产出互不重叠的输出变量，并在 `end` 屏障处原子汇合：

```yaml
# 1. 根节点输入契约
input:
  raw_draft: { type: string }

# 2. 根节点输出契约：由三个并发分支分别产出其中一个字段
output:
  formatted_doc: { type: string }
  compliance_report: { type: string }
  entity_summary: { type: string }

graph:
  nodes:
    # 并发分支 1（use 节点）：调用外部排版子工作流，产出 formatted_doc
    format_document:
      use: workflow:publishing/markdown_formatter
      with:
        input:
          content: raw_draft
          line_width: 100
        output:
          formatted_text: formatted_doc

    # 并发分支 2（run 节点）：执行单步合规扫描，产出 compliance_report
    scan_compliance:
      input: [raw_draft]
      output:
        compliance_report: { type: string }
      run: "扫描文稿 variables.raw_draft 的合规性，生成审计报告写入 output.compliance_report。"

    # 并发分支 3（内联 graph 节点）：在单个并发分支内封装两步串联逻辑，仅对外导出 entity_summary
    extract_entities_pipeline:
      input: [raw_draft]
      output: [entity_summary]
      graph:
        nodes:
          # 子图步骤 3.1：产出子图私有中间变量 raw_entities（不暴露至主图）
          ner_extract:
            input: [raw_draft]
            output:
              raw_entities:
                type: array
                items: { type: string }
            run: "从 variables.raw_draft 中抽取核心实体列表写入 output.raw_entities。"

          # 子图步骤 3.2：读取 raw_entities，产出对外导出的 entity_summary
          summarize_entities:
            input: [raw_entities]
            output:
              entity_summary: { type: string }
            run: "汇总实体列表 variables.raw_entities 生成术语表说明写入 output.entity_summary。"

        # 子图内部控制流：收敛至子图单出口 end
        edges:
          - start -> ner_extract
          - ner_extract -> summarize_entities
          - summarize_entities -> end

  # 3. 三元 Fork-Join 并发边：
  # - Fork：同时激活三个节点，各自读取 raw_draft 快照
  # - Disjoint Write：分别写回 formatted_doc、compliance_report、entity_summary
  # - Join：全部完成后原子合并写回主图 variables 并到达 end
  edges:
    - start -> [format_document, scan_compliance, extract_entities_pipeline] -> end
```

---

下一节：[01. 图栈帧与命名空间](../04-state-and-resilience/01-frames-and-scoping.md)。
