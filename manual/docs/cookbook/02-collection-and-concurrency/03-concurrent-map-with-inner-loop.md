# 配方 03：外层 `for_each` 并发遍历 + 内层 SESE 子图 `while` 循环

## 一、 场景与需求

并发处理一组章节或代码模块（外层 `for_each`），且每个元素内部需要独立运行多轮“翻译 → 审校 → 不合格则回边重译”的动态收敛循环（内层成环回边），需满足两项要求：

1. **元素间循环状态隔离**：并发执行的各个元素必须各自持有独立的循环计数器 `retry_count` 与修改意见 `revision_notes`，互不干扰。
2. **成功产物收集与失败原元素归档**：当某个元素在最大尝试次数内审校通过（`pass`）时，收集其译文至 `translated_chapters` 列表；当重试耗尽放弃（`drop`）时，不产出译文，并通过 [`builtin:assign`](../../reference/builtins/assign.md) 将原始章节结构体零拷贝归档至 `dropped_chapters` 列表。

---

## 二、 核心原语组合要点

1. **在 SESE 内联 `graph` 节点上挂载 `for_each`**：每次元素迭代均创建一个独立的块级栈帧（`Block Frame`），子图 `graph.variables` 中的 `retry_count` 与 `revision_notes` 严格限制在单次迭代帧内。
2. **子图迭代级分支与一阶集合提升对接**：内联 `graph` 声明 `pass`（`output: [translated_chapters]`）与 `drop`（`output: [dropped_chapters]`）两个分支，子图内部在 `drop` 分支串联 `builtin:assign` 零拷贝透传 `single_chapter` 后分别通过 `-> end.pass` 与 `-> end.drop` 退出，在外层屏障处按源顺序分别提升为两个列表变量：

```yaml
batch_translate:
  for_each:
    source: chapters
    item: single_chapter                      # 自动注入每次迭代的子图初始 variables
    concurrency: 3
  input: [target_language]                    # 显式捕获外层图的 target_language
  output:                                     # 显式声明外层目标变量名及其单次元素类型
    translated_chapters: { type: string }
    dropped_chapters:
      type: object
      properties:
        title: { type: string }
        body: { type: string }
      required: [title, body]
  branches:
    pass:
      description: "单章审校通过，导出译文至 translated_chapters"
      output: [translated_chapters]
    drop:
      description: "单章重试耗尽放弃，归档原章节至 dropped_chapters"
      output: [dropped_chapters]
  graph: ...
```

---

## 三、 完整工作流定义

```yaml
# 1. 根节点输入契约
input:
  target_language:
    type: string
  chapters:
    type: array
    items:
      type: object
      properties:
        title: { type: string }
        body: { type: string }
      required: [title, body]

# 2. 根节点输出契约
output:
  translated_chapters:
    type: array
    items: { type: string }
  dropped_chapters:
    type: array
    items:
      type: object
      properties:
        title: { type: string }
        body: { type: string }
      required: [title, body]

graph:
  nodes:
    # 外层并发遍历节点：挂载在 SESE 内联 graph 上
    batch_translate:
      for_each:
        source: chapters
        item: single_chapter
        concurrency: 3
      input: [target_language]
      output:
        translated_chapters: { type: string }
        dropped_chapters:
          type: object
          properties:
            title: { type: string }
            body: { type: string }
          required: [title, body]
      branches:
        pass:
          description: "单章审校通过，导出译文"
          output: [translated_chapters]
        drop:
          description: "单章重试耗尽放弃，导出原章节结构体"
          output: [dropped_chapters]

      # 每次元素迭代独立运行的块级子栈帧（Block Frame）
      graph:
        max_steps: 20
        # 单次迭代私有的跨轮次循环状态变量
        variables:
          revision_notes:
            type: string
            default: ""
          retry_count:
            type: integer
            default: 0

        nodes:
          # 子图步骤 1：根据原章节与上轮审校意见生成译文草稿，并递增 retry_count
          translate:
            input: [target_language, single_chapter, revision_notes, retry_count]
            output:
              draft_translation: { type: string }
              retry_count: { type: integer }
            run: "将章节《variables.single_chapter.title》正文 variables.single_chapter.body 译为 variables.target_language（结合修改意见 variables.revision_notes），写入 output.draft_translation，并将 variables.retry_count + 1 写入 output.retry_count。"

          # 子图步骤 2：审校译文草稿，决定通过 (pass)、回边重译 (rework) 或放弃 (drop)
          proofread:
            input: [draft_translation, retry_count]
            output:
              revision_notes: { type: string }
              translated_chapters: { type: string }
            branches:
              pass:
                description: "译文合格"
                output: [translated_chapters]
              rework:
                description: "存在瑕疵且 retry_count < 2，回边重译"
                output: [revision_notes]
              drop:
                description: "已满 2 次尝试仍不合格，放弃该章"
                output: []
            run:
              - "审校译文 variables.draft_translation。"
              - "若合格，写入 output.translated_chapters 并选择 pass。"
              - "若不合格且 variables.retry_count < 2，将修改意见写入 output.revision_notes 并选择 rework；否则选择 drop。"

          # 子图步骤 3：仅在 drop 分支下，通过 builtin:assign 零拷贝将原始 single_chapter 归档至 dropped_chapters
          archive_dropped_chapter:
            use: builtin:assign
            with:
              input:
                source: single_chapter
              output:
                value: dropped_chapters

        # 子图内部控制流：rework 构成成环回边，pass 与 drop 分别走向子图分支终点
        edges:
          - start -> translate
          - translate -> proofread
          - proofread.rework -> translate
          - proofread.pass -> end.pass
          - proofread.drop -> archive_dropped_chapter
          - archive_dropped_chapter -> end.drop

  # 主图控制流：batch_translate 内部消费 pass / drop 分支，对外为单条无分支出边
  edges:
    - start -> batch_translate
    - batch_translate -> end
```
