# 配方 02：并发多路分流、条件收集与扁平化汇总（`filter` + `collect` + `flatten`）

## 场景与问题

批量并发处理一批用户反馈 `batch.feedbacks`：
1. **原元素零拷贝过滤（`filter`）**：若某条反馈涉及严重线上故障（走 `urgent` 分支），直接将该原始反馈结构体零拷贝保留至 `urgent_feedbacks` 列表，并额外生成紧急工单 `urgent_ticket`；若属垃圾信息（走 `spam` 分支），直接跳过不产出任何字段。
2. **分支条件收集与单层扁平化（`collect` + `flatten: true`）**：所有有效反馈（`urgent` 与 `normal`）均产出结构化分析结果 `single_analysis` 和标签子列表 `extracted_tags`（展开拼接为一维总表 `all_feature_tags`）。

---

## 完整工作流定义

```yaml
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
    # 1. 并发遍历：同时使用 filter 零拷贝保留、collect 条件收集与 flatten 展开拼接
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
          description: "无意义垃圾信息，直接丢弃"
          output: []
      run:
        - "分析第 variables.idx 条反馈（用户 variables.feedback.user_id）：variables.feedback.content。"
        - "若为垃圾信息，直接选 spam 分支。"
        - "构造结构化分析写入 output.single_analysis，提取诉求标签列表写入 output.extracted_tags。"
        - "若属严重线上故障，生成工单描述写入 output.urgent_ticket 并选 urgent；否则选 normal。"

    # 2. 汇总阶段（Reduce）
    summarize_all:
      input: [analyzed_items, urgent_feedbacks, urgent_tickets, all_feature_tags]
      output:
        executive_summary: { type: string }
      run: "综合分析结果 variables.analyzed_items、高优原始反馈 variables.urgent_feedbacks、紧急工单 variables.urgent_tickets 与标签总表 variables.all_feature_tags，生成汇总报告写入 output.executive_summary。"

  edges:
    - start -> analyze_each
    - analyze_each -> summarize_all
    - summarize_all -> end
```
