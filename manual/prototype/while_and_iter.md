# 循环与遍历基础模式（`while`、`builtin:*` 与统一 `for_each`）

本文提供基础控制流与集合处理模式的标准示例，分别展示：
1. **`while` 动态条件收敛循环（通过 CFG 回边、显式 `input` 捕获与分支关联输出契约实现）**
2. **基于 `for_each.state` 的有状态串行累积折叠（Sequential Fold / Stateful Iteration，含 `builtin:unwrap` 与静态字段路径投影）**
3. **基于 `for_each.filter` 与 `for_each.collect` 的无状态并发集合遍历（涵盖按迭代分支零拷贝保留原元素的 `filter`、在 `run` 内转换并按分支条件收集的 `collect`、以及 `flatten: true` 单层扁平化收集）**

关于运行时内建算子（`builtin:unwrap`、`builtin:switch`、`builtin:array_shift`、`builtin:array_append`、`builtin:map_set`）的完整契约请参见 `builtins.md`；关于运行时异常与容错模型请参见 `error_handling.md`；关于外层并发遍历集合 + 内层条件循环的 SESE 块级嵌套组合模式，请参见 `while_plus_iter.md`。

---

## 示例一：`while` 动态条件收敛循环（CFG 回边 + 分支关联输出契约）

**场景：自动生成 SQL 并通过沙箱校验（最多重试 3 次）**

在图级符号表与分支敏感定值分析模型下，`while` 循环专门面向**事先未知迭代次数的动态条件收敛流程**，直接通过控制边连回上游节点即可。环路中的节点通过 `input: [...]` 显式声明读取的变量；配合**分支关联输出契约（`branches.<choice>.output`）**，成功分支（`success`）与失败早退分支（`failed`）可各自声明按需产出的字段子集，无需为未执行的分支填塞假初值：

```yaml
input:
  question:
    type: string
    description: "用户的自然语言查询需求（自动进入 variables.question）"
  schema:
    type: string
    description: "数据库表结构说明（自动进入 variables.schema）"
output:
  sql:
    type: string
    description: "校验通过的 SQL 语句（仅在 success 分支产出）"
  attempts:
    type: integer
    description: "总共尝试生成的次数"
  last_error:
    type: string
    description: "最后一次沙箱报错信息（仅在 failed 分支产出）"
branches:
  success:
    description: "SQL 生成并校验通过"
    output: [sql, attempts]
  failed:
    description: "达到最大重试次数仍存在语法或执行错误"
    output: [attempts, last_error]

graph:
  max_steps: 20
  # 声明跨循环轮次演进的初始状态
  variables:
    attempts: 0
    last_error: ""

  nodes:
    # 1. 循环体起点：生成或修正 SQL
    generate_sql:
      input: [schema, question, last_error, attempts]
      output:
        sql:
          type: string
        attempts:
          type: integer
      run:
        - "根据表结构 variables.schema 和问题 variables.question 编写 SQL。"
        - "如果 variables.last_error 不为空，请结合该错误信息修正上一版 SQL。"
        - "将结果写入 output.sql，并将 variables.attempts 加一后写入 output.attempts。"

    # 2. 循环条件判断点（使用分支关联输出：valid 分支无需覆写 last_error）
    validate_sql:
      input: [sql, attempts]
      output:
        last_error:
          type: string
      branches:
        valid:
          description: "SQL 语法正确且执行无报错"
          output: []
        retry:
          description: "执行报错，但未超过 3 次尝试上限，打回重写"
          output: [last_error]
        exhausted:
          description: "执行报错，且 variables.attempts 已达到 3 次"
          output: [last_error]
      run:
        - "在沙箱中试运行 variables.sql。"
        - "若成功，直接选择 valid 分支。"
        - "若失败，将具体报错信息写入 output.last_error；若 variables.attempts < 3 则选择 retry 分支，否则选择 exhausted 分支。"

  edges:
    - start -> generate_sql
    - generate_sql -> validate_sql
    # CFG 回边：构成 while 循环，携带更新后的 last_error 跳回 generate_sql
    - validate_sql.retry -> generate_sql
    # 循环出口（各出口分支仅校验其关联的 output 子集定值）
    - validate_sql.valid -> end.success
    - validate_sql.exhausted -> end.failed
```

---

## 示例二：有状态串行累积折叠（`for_each.state` + `for_each.collect` Sequential Fold）

**场景：按章节顺序逐章阅读长篇小说结构体 `book`，若提供可选的背景设定 `book.world_lore` 则先通过 `builtin:unwrap` 拆包融入初始摘要；随后跨章持续滚动更新“全局人物关系与剧情摘要”（第 $i$ 章的处理依赖第 $i-1$ 章更新后的摘要），同时将每一章生成的分章札记收集到列表 `chapter_notes` 中**

当遍历集合的每次迭代之间存在**串行状态依赖**时，直接使用 `for_each.state`（强制 `concurrency: 1`）：
* `current_chapter` 与 `chapter_idx` 严格注入在单次迭代局部帧内，不污染外层图的 `variables` 符号表，也无需手写 `cursor: 0` 游标；
* `state: [rolling_summary]` 声明跨元素原地累积更新的外层已定值变量，第 $i$ 次迭代自动读取第 $i-1$ 次迭代更新后的 `rolling_summary`；
* 同时配合 `collect: { single_note: chapter_notes }` 收集每章产出的阅读札记。

```yaml
input:
  book:
    type: object
    properties:
      title:
        type: string
      world_lore:
        type: [string, "null"]
      chapters:
        type: array
        items:
          type: string
    required: [title, world_lore, chapters]
    description: "书籍结构体，含书名、可选的世界观设定及按顺序排列的各章节正文列表"
output:
  rolling_summary:
    type: string
    description: "读完全书后最终累积的人物关系与剧情总摘要"
  chapter_notes:
    type: array
    items:
      type: string
    description: "按章节顺序收集的各章阅读札记列表"

graph:
  variables:
    rolling_summary: "（初始状态：尚未开始阅读）"

  nodes:
    # 0. 内建可空拆包算子：检查 book.world_lore 是否非空，将其从 [string, "null"] 收窄为非空 string
    check_lore:
      use: builtin:unwrap
      with:
        input:
          source: book.world_lore
        output:
          value: world_lore_text

    # 0b. 若存在世界观设定，先初始化到 rolling_summary 中
    init_lore_summary:
      input: [book, world_lore_text]
      output:
        rolling_summary:
          type: string
      run: "根据书名 variables.book.title 与世界观设定 variables.world_lore_text，生成初始背景摘要写入 output.rolling_summary。"

    # 1. 有状态串行折叠：直接投影读取 book.chapters，跨章原地演进 rolling_summary 并收集 single_note
    read_chapters:
      for_each:
        source: book.chapters
        item: current_chapter
        index: chapter_idx
        concurrency: 1
        state: [rolling_summary]
        collect:
          single_note: chapter_notes
      input: [book, chapter_idx, current_chapter, rolling_summary]
      output:
        rolling_summary:
          type: string
        single_note:
          type: string
      run:
        - "阅读《variables.book.title》的第 variables.chapter_idx 章内容：variables.current_chapter。"
        - "结合截至上一章的累积摘要 variables.rolling_summary，融入本章新增的人物动态与核心事件，将更新后的累积摘要写入 output.rolling_summary。"
        - "针对本章结合前文伏笔撰写单章阅读札记，写入 output.single_note。"

  edges:
    - start -> check_lore
    - check_lore.some -> init_lore_summary
    - init_lore_summary -> read_chapters
    - check_lore.none -> read_chapters
    - read_chapters -> end
```

---

## 示例三：无状态并发集合遍历（同时演示 `for_each.filter` 原元素零拷贝过滤与 `for_each.collect` 分支条件收集）

**场景：批量并发分析用户反馈列表。每条反馈：(1) 若属高优故障则走 `urgent` 分支，由 `for_each.filter` 零拷贝保留原始反馈结构体到 `urgent_feedbacks`，并额外生成紧急工单描述 `urgent_ticket`；若属普通反馈则走 `normal` 分支；若属垃圾信息则走 `spam` 分支直接丢弃；(2) 所有有效反馈（`urgent` 与 `normal`）均产出结构化情感分析结果 `single_analysis` 与功能诉求标签列表 `extracted_tags`（通过 `flatten: true` 展开拼接为一维总列表）**

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
    description: "待分析的用户反馈批次结构体"
output:
  urgent_feedbacks:
    type: array
    items:
      type: object
      properties:
        user_id: { type: string }
        content: { type: string }
      required: [user_id, content]
    description: "零拷贝保留的高优故障原始反馈结构体列表"
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
    items:
      type: string
  all_feature_tags:
    type: array
    items:
      type: string
  executive_summary:
    type: string

graph:
  nodes:
    # 1. 集合并发遍历节点（单次迭代消费 branches，同时完成原元素 Filter、分支条件 Collect 与扁平化 Collect）
    analyze_each:
      for_each:
        source: batch.feedbacks # 通过结构体字段路径投影绑定源数组
        item: feedback          # 注入单次迭代帧的元素（TypedDict，内部可通过 feedback.user_id / feedback.content 投影读取）
        index: idx              # 注入单次迭代帧的当前序号 (integer)
        concurrency: 4          # 最多 4 个任务并发执行
        # (a) 零拷贝原元素过滤（Filter）：当单次迭代选择 urgent 分支时，直接将原始 feedback 结构体保留至 urgent_feedbacks
        filter:
          urgent: urgent_feedbacks
        # (b) 节点产出收集提升（Collect）：自动仅从产出了对应字段的分支迭代中收集非空结果
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
          items:
            type: string
      branches:
        urgent:
          description: "有效反馈且涉及严重线上故障，需保留原反馈并额外生成紧急工单"
          output: [single_analysis, urgent_ticket, extracted_tags]
        normal:
          description: "普通有效反馈，仅产出情感分析与功能标签"
          output: [single_analysis, extracted_tags]
        spam:
          description: "无意义垃圾信息，直接跳过不产出任何字段"
          output: []
      run:
        - "审阅第 variables.idx 条反馈（用户 variables.feedback.user_id）：variables.feedback.content。"
        - "若为垃圾信息，直接选择 spam 分支。"
        - "提取情感倾向（positive/neutral/negative），连同 variables.idx 和 variables.feedback.user_id 构造对象写入 output.single_analysis；提取功能诉求标签列表写入 output.extracted_tags。"
        - "若反馈涉及严重线上故障，生成工单描述写入 output.urgent_ticket 并选择 urgent 分支；否则选择 normal 分支。"

    # 2. 汇总节点（Reduce 阶段）
    summarize_all:
      input: [analyzed_items, urgent_feedbacks, urgent_tickets, all_feature_tags]
      output:
        executive_summary:
          type: string
      run: "综合全体情感分析 variables.analyzed_items、高优原始反馈 variables.urgent_feedbacks、高优工单列表 variables.urgent_tickets 以及扁平化汇总后的功能标签 variables.all_feature_tags，撰写总体报告写入 output.executive_summary。"

  edges:
    - start -> analyze_each
    - analyze_each -> summarize_all
    - summarize_all -> end
```

---

## 设计正交性总结

1. **控制流与集合原语职责完全正交**：
   * **`while` 条件循环（回边）**：专职处理**未知迭代次数的动态条件收敛控制流**（如生成-校验重试、满足条件即停），配合分支关联输出契约（`branches.<choice>.output`）原地演进图级状态。
   * **`for_each`（统一局部帧集合迭代：`filter` + `collect` + `state`）**：专职处理**有限集合的元素级遍历**，通过 `source -> item/key/index` 统一注入单次迭代局部帧，在内部消费单次迭代的 `branches`（及 `catch` 异常分支），并在输出端正交支持：
     1. **`filter`**：按迭代分支零拷贝保留原元素（`Array<T> -> Array<T>`）；
     2. **`collect`**：收集节点内部转换产出的字段并提升为集合（`Array<T> -> Array<U>`，自动只收集产出该字段的分支，并支持 `flatten: true` 单层展开拼接）；
     3. **`state`**：有状态串行累积折叠（`fold`，强制 `concurrency: 1`）。
   * **`builtin:*`（零开销确定性算子）**：专职处理外部输入的 `Optional<T>` 拆包/默认值合并（`builtin:unwrap`）、标量/枚举确定性分支路由（`builtin:switch`）以及 `while` 循环内的集合拆解消费与增量更新（`builtin:array_shift` / `builtin:array_append` / `builtin:map_set`）。
2. **显式捕获与无遮蔽局部帧**：图内 `run` 与内联 `graph` 通过 `input: [...]` 显式声明捕获变量，单次迭代内部使用单数语义变量名（`feedback`、`single_analysis`），外层图使用复数集合变量名（`urgent_feedbacks`、`analyzed_items`），静态类型表在任意作用域内均满足“同名同类型”。
