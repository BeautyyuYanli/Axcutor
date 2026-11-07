# 配方 03：外层 `for_each` 并发遍历 + 内层 SESE 子图 `while` 循环

## 一、 场景与需求

在批量处理一组章节或代码模块时，往往需要**外层高并发扇出**与**内层单元素多轮迭代收敛**协同工作：外层通过 `for_each` 并发分发各个章节，而每个章节内部需要独立运行“翻译 → 计数 → 审校 → 不合格则回边重译”的动态收敛循环（内层成环回边）。该架构需满足三项核心工程要求：

1. **外层并发隔离与内层 `Block Frame` 独立循环状态**：并发执行的各个章节在各自的块级栈帧（`Block Frame`）中运行，各自持有私有的循环计数器 `retry_count` 与审校意见 `revision_notes`，从根源上杜绝并发状态串扰。
2. **业务生成与确定性状态演进解耦**：翻译节点仅专注文本生成，尝试次数 `retry_count` 的递增交由 [`builtin:math`](../../reference/builtins/math.md) 算子在子图内确定性执行，避免让业务执行器顺带承担算术计数职责。
3. **成功产物收集与放弃项零拷贝归档**：当某个章节在最大尝试次数内审校通过（`pass`）时，将其译文收集至 `translated_chapters` 列表；当重试耗尽放弃（`drop`）时，不产出译文，而是通过 [`builtin:assign`](../../reference/builtins/assign.md) 将原始章节结构体零拷贝归档至 `dropped_chapters` 列表。

---

## 二、 核心原语组合要点

1. **外层 `for_each` 挂载于 SESE 内联 `graph` 节点**：`batch_translate` 将 `for_each` 直接挂载在内联 `graph` 上。每次元素迭代均实例化一个独立的块级栈帧（`Block Frame`）：当前章节自动注入为帧内只读初始变量 `single_chapter`，而子图 `graph.variables` 中声明的 `retry_count` 与 `revision_notes` 严格局限于单次迭代帧生命周期内，跨轮次重译时原地更新、跨并发元素间完全隔离。
2. **内层确定性算术计数（`builtin:math`）**：在 `translate` 与 `proofread` 之间串联 `builtin:math`（`op: { value: "add" }`），在每次生成译文草稿后对当前栈帧的 `retry_count` 执行原地自增，为下游 `proofread` 提供精确、无幻觉的轮次判定依据。
3. **子图迭代级分支、`builtin:assign` 零拷贝归档与一阶集合提升**：内联 `graph` 显式声明 `pass`（`output: [translated_chapters]`）与 `drop`（`output: [dropped_chapters]`）两个出口分支。当审校通过时直接沿 `proofread.pass -> end.pass` 导出译文；当重试耗尽进入 `proofread.drop` 时，经由 `builtin:assign` 将帧内 `single_chapter` 零拷贝绑定至 `dropped_chapters` 后从 `end.drop` 退出。外层同步屏障在所有并发帧收敛后，按输入 `chapters` 的原始顺序分别将两路产物聚合成顶层数组。

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
          # 子图步骤 1：根据原章节与上轮审校意见生成译文草稿
          translate:
            input: [target_language, single_chapter, revision_notes]
            output:
              draft_translation: { type: string }
            run: "将章节《variables.single_chapter.title》正文 variables.single_chapter.body 译为 variables.target_language（结合修改意见 variables.revision_notes），写入 output.draft_translation。"

          # 子图步骤 2：使用 builtin:math 确定性地将当前迭代帧的 retry_count 加 1
          increment_retry:
            use: builtin:math
            with:
              input:
                op: { value: "add" }
                left: retry_count
                right: 1
              output:
                result: retry_count

          # 子图步骤 3：审校译文草稿，决定通过 (pass)、回边重译 (rework) 或放弃 (drop)
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
            run: "审校译文 variables.draft_translation。若合格，写入 output.translated_chapters 并选择 pass；若不合格且 variables.retry_count < 2，将修改意见写入 output.revision_notes 并选择 rework；否则选择 drop。"

          # 子图步骤 4：仅在 drop 分支下，通过 builtin:assign 零拷贝将原始 single_chapter 归档至 dropped_chapters
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
          - translate -> increment_retry
          - increment_retry -> proofread
          - proofread.rework -> translate
          - proofread.pass -> end.pass
          - proofread.drop -> archive_dropped_chapter
          - archive_dropped_chapter -> end.drop

  # 主图控制流：batch_translate 内部消费 pass / drop 分支，对外为单条无分支出边
  edges:
    - start -> batch_translate
    - batch_translate -> end
```
