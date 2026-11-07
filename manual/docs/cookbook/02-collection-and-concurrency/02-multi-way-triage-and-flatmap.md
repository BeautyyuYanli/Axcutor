# 配方 02：并发多路分流、条件收集与扁平化汇总（`for_each` + `branches.output` + `array_flatten`）

## 一、 场景与需求

批量并发处理用户反馈列表 `batch.feedbacks` 时，需在遍历中同时完成三项数据汇总：

1. **原元素零拷贝保留**：若反馈涉及严重线上故障，走 `urgent` 分支，通过 [`builtin:assign`](../../reference/builtins/assign.md) 将原始反馈结构体零拷贝保留至 `urgent_feedbacks` 列表，并额外生成紧急工单 `urgent_tickets`；若属垃圾信息，走 `spam` 分支，直接跳过且不产出任何字段。
2. **按分支条件收集**：`urgent` 与 `normal` 分支均产出结构化情感分析结果，经一阶同构集合提升汇聚为 `analyzed_items` 列表；紧急工单则仅从 `urgent` 分支收集为 `urgent_tickets` 列表。
3. **二维列表单层展平**：每条有效反馈产出的标签列表 `Array<string>` 经 `for_each` 一阶提升为二维列表 `nested_feature_tags`，再通过 [`builtin:array_flatten`](../../reference/builtins/array-flatten.md) 按原序展开拼接为一维总表 `all_feature_tags`。

---

## 二、 核心原语组合要点

1. **在 `for_each` 节点上组合 `output` 与 `branches.<branch>.output`**：在 `output` 中直接声明外层集合变量名及其单次迭代元素类型 $U$，并通过 `branches.<branch>.output` 限定各分支产出的字段子集；当单次迭代以 `end.spam` 结束时，因其声明 `output: []`，屏障汇聚会自动跳过该迭代而不向任何外层列表追加元素。
2. **在内联 `graph` 中串联 `builtin:assign` 实现原元素零拷贝透传**：在子图的 `triage_single.urgent` 分支后串联 `builtin:assign`，将迭代绑定变量 `feedback` 直接赋值给 `urgent_feedbacks`，无需执行器在结构化输出中重复拷贝输入结构体。
3. **在 `for_each` 后串联 `builtin:array_flatten` 完成二维列表展平**：`analyze_each` 屏障汇聚完成后，将一阶提升产出的二维中间列表 `nested_feature_tags` 传入 `builtin:array_flatten`，展平为一维总表 `all_feature_tags` 供下游汇总节点消费。

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
  # 3. 显式声明由阶段 1 for_each 一阶提升产出的二维标签中间列表变量 nested_feature_tags
  # （urgent_feedbacks、analyzed_items、urgent_tickets、all_feature_tags 已在顶层 output 中声明）
  variables:
    nested_feature_tags:
      type: array
      items:
        type: array
        items: { type: string }

  nodes:
    # 阶段 1（并发分流与一阶集合提升）：并发遍历 batch.feedbacks
    # 内部消费 urgent / normal / spam 三个分支，按分支子集汇聚四个外层列表变量
    analyze_each:
      for_each:
        source: batch.feedbacks
        item: feedback
        index: idx
        concurrency: 4
      output:
        urgent_feedbacks:
          type: object
          properties:
            user_id: { type: string }
            content: { type: string }
          required: [user_id, content]
        analyzed_items:
          type: object
          properties:
            index: { type: integer }
            user_id: { type: string }
            sentiment:
              type: string
              enum: [positive, neutral, negative]
          required: [index, user_id, sentiment]
        urgent_tickets:
          type: string
        nested_feature_tags:
          type: array
          items: { type: string }
      branches:
        urgent:
          description: "严重线上故障，保留原反馈并额外产出紧急工单"
          output: [urgent_feedbacks, analyzed_items, urgent_tickets, nested_feature_tags]
        normal:
          description: "普通有效反馈，仅产出情感分析与标签"
          output: [analyzed_items, nested_feature_tags]
        spam:
          description: "无意义垃圾信息，不产出任何字段"
          output: []
      graph:
        nodes:
          # 子图步骤 1.1：评估单条反馈，产出分析字段与标签列表，并选择分支
          triage_single:
            input: [idx, feedback]
            output:
              analyzed_items:
                type: object
                properties:
                  index: { type: integer }
                  user_id: { type: string }
                  sentiment:
                    type: string
                    enum: [positive, neutral, negative]
                required: [index, user_id, sentiment]
              urgent_tickets:
                type: string
              nested_feature_tags:
                type: array
                items: { type: string }
            branches:
              urgent:
                description: "严重线上故障"
                output: [analyzed_items, urgent_tickets, nested_feature_tags]
              normal:
                description: "普通有效反馈"
                output: [analyzed_items, nested_feature_tags]
              spam:
                description: "无意义垃圾信息"
                output: []
            run:
              executor: builtin
              content: |
                text = input.feedback.content.strip()
                if not text or "spam" in text.lower():
                    return Branch.spam()
                if "crash" in text.lower() or "outage" in text.lower():
                    return Branch.urgent(
                        analyzed_items={
                            "index": input.idx,
                            "user_id": input.feedback.user_id,
                            "sentiment": "negative",
                        },
                        urgent_tickets=f"INC-{input.idx}:{input.feedback.user_id}",
                        nested_feature_tags=["reliability", "incident"],
                    )
                return Branch.normal(
                    analyzed_items={
                        "index": input.idx,
                        "user_id": input.feedback.user_id,
                        "sentiment": "neutral",
                    },
                    nested_feature_tags=["general"],
                )

          # 子图步骤 1.2：仅在 urgent 分支下，通过 builtin:assign 零拷贝透传原始 feedback 结构体
          keep_urgent_feedback:
            use: builtin:assign
            with:
              input:
                source: feedback
              output:
                value: urgent_feedbacks

        edges:
          - start -> triage_single
          - triage_single.urgent -> keep_urgent_feedback
          - keep_urgent_feedback -> end.urgent
          - triage_single.normal -> end.normal
          - triage_single.spam -> end.spam

    # 阶段 2（二维列表展平）：将二维标签列表 nested_feature_tags 展平为一维总表 all_feature_tags
    flatten_tags:
      use: builtin:array_flatten
      with:
        input:
          source: nested_feature_tags
        output:
          result: all_feature_tags

    # 阶段 3（汇总报告）：读取汇聚与展平后的四个列表变量，生成总体报告
    summarize_all:
      input: [analyzed_items, urgent_feedbacks, urgent_tickets, all_feature_tags]
      output:
        executive_summary: { type: string }
      run:
        executor: builtin
        content: |
          summary = (
              f"Analyzed={len(input.analyzed_items)}, "
              f"Urgent={len(input.urgent_tickets)}, "
              f"Tags={len(input.all_feature_tags)}"
          )
          return Output(executive_summary=summary)

  # 4. 外层控制流边：analyze_each 内部消化所有迭代分支，对外为单条无分支出边
  edges:
    - start -> analyze_each
    - analyze_each -> flatten_tags
    - flatten_tags -> summarize_all
    - summarize_all -> end
```
