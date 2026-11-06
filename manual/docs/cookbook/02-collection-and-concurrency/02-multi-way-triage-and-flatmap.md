# 配方 02：并发多路分流、条件收集与扁平化汇总（`filter` + `collect` + `flatten`）

## 一、 场景与需求

批量并发处理用户反馈列表 `batch.feedbacks` 时，需在单次遍历中同时完成三项数据汇总：

1. **原元素零拷贝过滤（`filter`）**：若某条反馈涉及严重线上故障（走 `urgent` 分支），将该原始反馈结构体零拷贝保留至 `urgent_feedbacks` 列表，并额外生成紧急工单 `urgent_ticket`；若属无效垃圾信息（走 `spam` 分支），直接跳过且不产出任何字段。
2. **分支条件收集（`collect`）**：所有有效反馈（`urgent` 与 `normal` 分支）均产出结构化情感分析结果 `single_analysis`，汇聚为 `analyzed_items` 列表；`urgent_ticket` 则仅从 `urgent` 分支收集为 `urgent_tickets` 列表。
3. **子列表单层扁平化（`flatten: true`）**：每条有效反馈产出的标签列表 `extracted_tags`（`Array<string>`）按原序展开拼接为一维总表 `all_feature_tags`（`Array<string>`）。

---

## 二、 核心原语组合要点

在同一个 `for_each` 节点上正交组合 `filter` 与 `collect`（各目标变量名互不重叠）：

```yaml
analyze_each:
  for_each:
    source: batch.feedbacks
    item: feedback
    index: idx
    concurrency: 4
    # 1. filter：将走 urgent 分支的原始 feedback 结构体保留至 urgent_feedbacks
    filter:
      urgent: urgent_feedbacks
    # 2. collect：按分支实际产出的非空字段执行收集与单层扁平化
    collect:
      single_analysis: analyzed_items   # 从 urgent 与 normal 分支收集
      urgent_ticket: urgent_tickets     # 仅从 urgent 分支收集
      extracted_tags:
        into: all_feature_tags
        flatten: true                   # 将 Array<string> 展开拼接为一维列表
```

---

## 三、 完整工作流定义

```yaml
# 1. 根节点输入契约
input:
  batch:
    type: object
    properties:
      feedbacks:
        type: array
        items:
          type: object
          properties:
            user_id: { type: string }
            content: { type: string }
          required: [user_id, content]
    required: [feedbacks]

# 2. 根节点输出契约
output:
  urgent_feedbacks:
    type: array
    items:
      type: object
      properties:
        user_id: { type: string }
        content: { type: string }
      required: [user_id, content]
  analyzed_items:
    type: array
    items:
      type: object
      properties:
        index: { type: integer }
        user_id: { type: string }
        sentiment:
          type: string
          enum: [positive, neutral, negative]
      required: [index, user_id, sentiment]
  urgent_tickets:
    type: array
    items: { type: string }
  all_feature_tags:
    type: array
    items: { type: string }
  executive_summary:
    type: string

graph:
  nodes:
    # 阶段 1（Map / Filter / FlatMap）：并发遍历 batch.feedbacks
    # 内部消费 urgent / normal / spam 三个分支，通过 filter 与 collect 产出四个外层列表变量
    analyze_each:
      for_each:
        source: batch.feedbacks
        item: feedback
        index: idx
        concurrency: 4
        filter:
          urgent: urgent_feedbacks
        collect:
          single_analysis: analyzed_items
          urgent_ticket: urgent_tickets
          extracted_tags:
            into: all_feature_tags
            flatten: true
      input: [idx, feedback]
      output:
        single_analysis:
          type: object
          properties:
            index: { type: integer }
            user_id: { type: string }
            sentiment:
              type: string
              enum: [positive, neutral, negative]
          required: [index, user_id, sentiment]
        urgent_ticket:
          type: string
        extracted_tags:
          type: array
          items: { type: string }
      branches:
        urgent:
          description: "严重线上故障，保留原反馈并额外产出紧急工单"
          output: [single_analysis, urgent_ticket, extracted_tags]
        normal:
          description: "普通有效反馈，仅产出情感分析与标签"
          output: [single_analysis, extracted_tags]
        spam:
          description: "无意义垃圾信息，不产出任何字段"
          output: []
      run:
        - "分析第 variables.idx 条反馈（用户 variables.feedback.user_id）：variables.feedback.content。"
        - "若为垃圾信息，直接选择 spam 分支。"
        - "构造结构化分析写入 output.single_analysis，提取诉求标签列表写入 output.extracted_tags。"
        - "若属严重线上故障，生成工单描述写入 output.urgent_ticket 并选择 urgent；否则选择 normal。"

    # 阶段 2（Reduce）：读取第一阶段汇聚的四个列表变量，生成总体报告
    summarize_all:
      input: [analyzed_items, urgent_feedbacks, urgent_tickets, all_feature_tags]
      output:
        executive_summary: { type: string }
      run: "综合分析结果 variables.analyzed_items、高优原始反馈 variables.urgent_feedbacks、紧急工单 variables.urgent_tickets 与标签总表 variables.all_feature_tags，生成汇总报告写入 output.executive_summary。"

  # 3. 外层控制流边：analyze_each 内部消化所有迭代分支，对外为单条无分支出边
  edges:
    - start -> analyze_each
    - analyze_each -> summarize_all
    - summarize_all -> end
```
