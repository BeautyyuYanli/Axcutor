# 配方 03：外层 `for_each` 并发遍历 + 内层 SESE 子图 `while` 循环

## 场景与问题

当我们需要并发处理一组章节或代码模块（外层 `for_each`），而**每一个元素内部都需要独立运行一个多步“处理 → 审校/测试 → 若不合格则回边重试”的 `while` 循环**时：
* 如何确保并发执行的各个元素之间的循环计数器 `retry_count` 和中间草稿完全隔离？
* 当某个元素重试耗尽后决定放弃（`drop`）时，如何只收集成功元素的非空产物，并同时零拷贝归档被放弃的原始元素？

## 解决方案要点

* 直接将 `for_each` 挂载在 **SESE 内联 `graph` 节点**上！
* 每次迭代都会启动一个独立的块级子栈帧（`Block Frame`），子图内部的 `retry_count` 与 `revision_notes` 天然隔离；
* 内联 `graph` 使用 `output: [chapter_translation]` 列表简写，对外声明 `pass`（`output: [chapter_translation]`）与 `drop`（`output: []`）两个迭代级分支，由外层 `for_each.collect` 和 `for_each.filter` 分别完成非空产物收集与原章节零拷贝归档。

---

## 完整工作流定义

```yaml
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
    batch_translate:
      for_each:
        source: chapters
        item: single_chapter          # 自动注入每次迭代的子图初始 variables
        concurrency: 3
        filter:
          drop: dropped_chapters      # 零拷贝保留走 drop 分支的原始章节结构体
        collect:
          chapter_translation: translated_chapters # 自动仅收集走 pass 分支产出的非空 string
      input: [target_language]        # 显式捕获外层图的 target_language
      output: [chapter_translation]   # 列表简写：自动从子图符号表推导类型
      branches:
        pass:
          description: "单章审校通过，导出译文"
          output: [chapter_translation]
        drop:
          description: "单章重试耗尽放弃，不导出译文"
          output: []

      # SESE 块级子栈帧：单章内部的 while 循环控制流图
      graph:
        max_steps: 20
        variables:
          revision_notes: ""
          retry_count: 0

        nodes:
          translate:
            input: [target_language, single_chapter, revision_notes, retry_count]
            output:
              draft_translation: { type: string }
              retry_count: { type: integer }
            run: "将章节《variables.single_chapter.title》正文 variables.single_chapter.body 译为 variables.target_language（结合修改意见 variables.revision_notes），写入 output.draft_translation，并将 variables.retry_count + 1 写入 output.retry_count。"

          proofread:
            input: [draft_translation, retry_count]
            output:
              revision_notes: { type: string }
              chapter_translation: { type: string }
            branches:
              pass:
                description: "译文合格"
                output: [chapter_translation]
              rework:
                description: "有瑕疵且 retry_count < 2，打回重译"
                output: [revision_notes]
              drop:
                description: "已满 2 次重试仍不合格，放弃该章"
                output: []
            run:
              - "审校译文 variables.draft_translation。"
              - "若合格，写入 output.chapter_translation 并选 pass。"
              - "若不合格且 variables.retry_count < 2，将意见写入 output.revision_notes 并选 rework；否则选 drop。"

        edges:
          - start -> translate
          - translate -> proofread
          - proofread.rework -> translate
          - proofread.pass -> end.pass
          - proofread.drop -> end.drop

  edges:
    - start -> batch_translate
    - batch_translate -> end
```
