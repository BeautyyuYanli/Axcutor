# 01. 同构结构化并发：集合遍历（`for_each`）

当需要对图中某个非空强类型列表（`type: array`）或非空强类型有序映射（`type: object` + `additionalProperties`）的每个元素执行同构处理时，可在任意节点（`run`、`use` 或内联 `graph`）上挂载 **`for_each`** 修饰符。

`for_each` 为每次元素迭代创建独立的局部隔离帧（`Iteration Frame`），在单次迭代内部消化节点的 `branches` 与 `catch`，对外层图的 `edges` 始终呈现为**单条无分支出边（`node -> target`）**，并在输出端提供三类正交数据汇聚通道：**`filter`**、**`collect`** 与 **`state`**。

---

## 一、 单次迭代局部帧注入（`source -> item / key / index`）

`for_each` 支持遍历列表与有序映射两类集合，并通过 `concurrency` 控制最大并发度（正整数，默认 `1`；未声明 `state` 时可设为 `> 1` 开启并发遍历）。

> [!NOTE]
> **三类节点在挂载 `for_each` 时的 `input` 声明规则**：
> * **`run`（`ActionNode`）**：节点 `input: [...]` **必须显式列出** `run` 所需读取的 `item` / `key` / `index`、`state` 及外层变量（未列入 `input` 的变量不会进入传给业务执行器的 `input_snapshot`）；
> * **`use`（`CallNode`）**：在 `with.input` 中直接引用 `item` / `key` / `index`（或其结构体字段路径）及外层变量；
> * **内联 `graph`（`InlineGraphNode`）**：由于 `item` / `key` / `index` 与 `state` 已自动注入子图的初始 `Block Frame`，内联 `graph` 节点的 `input: [...]` 仅用于捕获外层普通变量，**禁止**重复列出 `item` / `key` / `index` 或 `state`。

### 1. 遍历强类型列表（`type: array`）

* **`source`（必填）**：绑定已定值的非空列表变量或结构体字段路径（如 `batch.items`）。
* **`item`（必填）**：注入当前迭代帧的元素变量名（类型自动推导为列表 `items` 类型）。
* **`index`（可选）**：注入当前下标 `0, 1, ...`（类型 `integer`）；遍历列表时禁止声明 `key`。

```yaml
process_list_items:
  for_each:
    source: batch.items      # 源列表：{ type: array, items: { type: string } }
    item: current_item       # 注入当前元素（类型自动推导为 string）
    index: item_idx          # 注入当前下标 0, 1, ...（类型为 integer）
    concurrency: 4           # 最多同时并发执行 4 个元素迭代
    collect:
      processed: results
  # 节点 input 直接读取 for_each 注入的局部变量 item_idx 与 current_item
  input: [item_idx, current_item]
  output:
    processed: { type: string }
  run: "处理第 variables.item_idx 项 variables.current_item，写入 output.processed。"
```

### 2. 遍历强类型有序映射（`type: object` + `additionalProperties`）

* **`source`（必填）**：绑定已定值的非空有序映射变量或结构体字段路径，按 Ordered Map 插入顺序遍历。
* **`item`（必填）**：注入当前值（Value，类型自动推导为 `additionalProperties` 类型）。
* **`key`（可选）**：注入当前键（Key，类型 `string`）。
* **`index`（可选）**：注入当前序号 `0, 1, ...`（类型 `integer`）。

```yaml
translate_sections:
  for_each:
    source: section_map      # 源有序映射：{ type: object, additionalProperties: { type: string } }
    key: section_id          # 注入当前键（类型为 string）
    item: section_body       # 注入当前值（类型为 string）
    index: section_idx       # 注入当前序号（类型为 integer）
    concurrency: 4
    collect:
      translated_body: translated_section_map  # 自动推导为与 source 同构的有序映射
  input: [section_id, section_body]
  output:
    translated_body: { type: string }
  run: "翻译章节 variables.section_id 的正文 variables.section_body，写入 output.translated_body。"
```

---

## 二、 三类正交输出通道（`filter` / `collect` / `state`）

### 1. `filter`：按单次迭代分支零拷贝保留原元素（`Array<T> -> Array<T>`）

当节点声明了 `branches`（或 `catch`）时，`filter: { <branch>: <target_var> }` 会在单次迭代进入 `<branch>` 分支时，将该次迭代的原始输入元素 `item`（若源为有序映射则保留原键值对 `key: item`）按原顺序零拷贝收入外层变量 `<target_var>`：

```yaml
triage_articles:
  for_each:
    source: feed_payload.articles
    item: current_article
    concurrency: 5
    filter:
      keep: valid_articles      # 收集走 keep 分支的原始元素，类型与 source 一致
      flagged: review_queue     # 收集走 flagged 分支的原始元素，类型与 source 一致
  input: [current_article]
  branches:
    keep: "符合规范，保留"
    flagged: "疑似违规，转人工复核"
    drop: "垃圾内容，不进入任何 filter 目标变量"
  run: "审阅文章 variables.current_article.title，选择 keep、flagged 或 drop 分支。"
```

### 2. `collect`：收集单次迭代产出并提升为集合（`Array<T> -> Array<U>`）

节点单次迭代产出的字段 `<single_field>`（非 `state` 字段）仅存在于单次迭代帧内，通过 `collect` 汇聚提升为外层集合变量 `<target_var>`：

* **默认提升（`collect: { <single_field>: <target_var> }`）**：仅从所选分支产出了该字段的迭代中收集结果；源为列表时提升为 `Array<U>`，源为有序映射时保留原键提升为 `OrderedMap<string, U>`。
* **单层扁平化提升（`collect: { <single_field>: { into: <target_var>, flatten: true } }`）**：当源为列表且单次产出 `<single_field>` 本身也是列表 `Array<U>` 时，按迭代顺序展开拼接为一维列表 `Array<U>`。

```yaml
analyze_articles:
  for_each:
    source: valid_articles
    item: current_article
    index: idx
    concurrency: 5
    collect:
      score: all_scores                        # 收集所有迭代产出的 score -> Array<number>
      featured_summary: featured_summaries     # 仅收集走 high_quality 分支产出的非空 string -> Array<string>
      article_tags:
        into: all_tags
        flatten: true                          # 将每次迭代产出的 Array<string> 扁平化拼接为一维 Array<string>
  input: [idx, current_article]
  output:
    score: { type: number }
    featured_summary: { type: string }
    article_tags:
      type: array
      items: { type: string }
  branches:
    high_quality:
      description: "高分文章，额外产出精选摘要"
      output: [score, featured_summary, article_tags]
    standard:
      description: "普通文章，不产出精选摘要"
      output: [score, article_tags]
  run: "评估第 variables.idx 篇文章 variables.current_article.title，写入 output.score 与 output.article_tags；高分则写入 output.featured_summary 并选 high_quality，否则选 standard。"
```

### 3. `state`：跨元素有状态串行折叠（`Sequential Fold`）

当第 $i$ 次迭代需要读取并更新第 $i-1$ 次迭代累积的状态变量时，声明 `state: [<state_var>, ...]`。声明 `state` 时，`concurrency` 必须为 `1`，且 `<state_var>` 在进入该节点前必须已在所属图中定值。

```yaml
read_chapters_sequentially:
  for_each:
    source: book_data.chapters
    item: current_chapter
    index: chapter_idx
    concurrency: 1                # 使用 state 时必须为串行执行（concurrency: 1）
    state: [rolling_summary]      # 跨元素原地演进外层已定值变量 rolling_summary
    collect:
      single_note: chapter_notes  # 可同时配合 collect 收集每章独立产出的札记
  input: [chapter_idx, current_chapter, rolling_summary]
  output:
    rolling_summary: { type: string }
    single_note: { type: string }
  run: "阅读第 variables.chapter_idx 章 variables.current_chapter，结合上一章累积摘要 variables.rolling_summary 更新写入 output.rolling_summary，并撰写本章札记写入 output.single_note。"
```

当 `for_each.state` 与多分支（`branches`）或元素级异常捕获（`catch`）结合使用时，遵循以下静态与运行期规则：

* **正常分支的 `state` 完备性约束**：若节点同时声明了 `branches`，则**每一个正常业务分支**的 `output` 列表都必须包含全部 `state` 变量（若某正常分支无需改变某个 `state` 变量，在 `run` 中将原值写回 `output`，在内联 `graph` 中由于 `state` 已在入口定值，直接走向 `end.<branch>` 即可自动提交原值）。
* **`catch` 异常分支下的状态回滚与保持**：若第 $i$ 次迭代触发异常并被节点的 `catch` 捕获（当次尝试脏修改回滚、产出为空），累积状态 `state` 不会丢失，而是自动保留第 $i-1$ 次迭代完成时的有效快照值并传入第 $i+1$ 次迭代：

```yaml
fold_with_branch_and_catch:
  for_each:
    source: book_data.chapters
    item: current_chapter
    concurrency: 1
    state: [rolling_summary]
    filter:
      bad_chapter: failed_chapters  # 收集触发异常的章节
    collect:
      single_note: chapter_notes    # 仅收集 updated 分支产出的札记
  catch:
    bad_chapter: [execution, timeout]  # 异常迭代回滚，rolling_summary 自动保留上一章的值继续传给下一章
  input: [current_chapter, rolling_summary]
  output:
    rolling_summary: { type: string }
    single_note: { type: string }
  branches:
    updated:
      description: "正常提炼本章内容并产出札记"
      output: [rolling_summary, single_note]
    skipped:
      description: "跳过无关章节，不产出 single_note，但必须包含 state 变量 rolling_summary"
      output: [rolling_summary]
  run: "若章节有效则更新 variables.rolling_summary 并写 output.single_note 选 updated；若跳过则将原 variables.rolling_summary 原样写入 output.rolling_summary 选 skipped。"
```

---

## 三、 完整示例：并发分流过滤与结果收集流水线

以下完整工作流串联两个 `for_each` 节点：第一步对原始文章列表执行并发三路分流（`filter`），第二步对保留下来的合规文章并发提取评分、精选摘要与扁平化标签列表（`collect`）：

```yaml
# 1. 根节点输入契约
input:
  articles:
    type: array
    items:
      type: object
      properties:
        id: { type: string }
        title: { type: string }
        body: { type: string }
      required: [id, title, body]

# 2. 根节点输出契约
output:
  review_queue:
    type: array
    items:
      type: object
      properties:
        id: { type: string }
        title: { type: string }
        body: { type: string }
      required: [id, title, body]
  featured_summaries:
    type: array
    items: { type: string }
  all_tags:
    type: array
    items: { type: string }

graph:
  nodes:
    # 阶段 1：并发遍历原始 articles，按迭代分支使用 filter 零拷贝分流原元素
    # 注意：keep / flagged / drop 分支在 for_each 内部消费，外层 edges 仅写单条无分支出边
    triage_articles:
      for_each:
        source: articles
        item: current_article
        concurrency: 5
        filter:
          keep: valid_articles      # 产出一阶段中间列表 valid_articles
          flagged: review_queue     # 直接产出顶层输出所需的 review_queue
      input: [current_article]
      branches:
        keep: "内容合规，进入深度分析"
        flagged: "疑似违规，转入人工复核队列"
        drop: "无效内容，直接丢弃"
      run: "审核文章 variables.current_article.title 与正文，选择 keep、flagged 或 drop 分支。"

    # 阶段 2：并发遍历 valid_articles，使用 collect 收集单次迭代产出并做单层扁平化
    analyze_valid_articles:
      for_each:
        source: valid_articles
        item: current_article
        index: idx
        concurrency: 5
        collect:
          featured_summary: featured_summaries  # 仅从走 high_quality 分支的迭代中收集非空摘要
          article_tags:
            into: all_tags
            flatten: true                       # 将每篇产出的标签子列表展开合并为一维列表 all_tags
      input: [idx, current_article]
      output:
        featured_summary: { type: string }
        article_tags:
          type: array
          items: { type: string }
      branches:
        high_quality:
          description: "优质文章，产出精选摘要与标签"
          output: [featured_summary, article_tags]
        standard:
          description: "常规文章，仅产出标签"
          output: [article_tags]
      run: "分析第 variables.idx 篇文章 variables.current_article.body，提取标签写入 output.article_tags；若属优质文章则额外撰写摘要写入 output.featured_summary 并选 high_quality，否则选 standard。"

  # 3. 外层控制流边：挂载了 for_each 的节点对外均为单一无分支出边
  edges:
    - start -> triage_articles
    - triage_articles -> analyze_valid_articles
    - analyze_valid_articles -> end
```

> 更多集合与并发组合模式可参阅 Cookbook：
> * [有状态串行折叠](../../cookbook/02-collection-and-concurrency/01-stateful-sequential-fold.md)
> * [并发多路分流与扁平化](../../cookbook/02-collection-and-concurrency/02-multi-way-triage-and-flatmap.md)
> * [外层 `for_each` 并发遍历 + 内层 SESE 子图循环](../../cookbook/02-collection-and-concurrency/03-concurrent-map-with-inner-loop.md)

---

下一节：[02. 异构结构化并发：Fork-Join 并发组（`-> [a, b] ->`）](./02-fork-join.md)。
