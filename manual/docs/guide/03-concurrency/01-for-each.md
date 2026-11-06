# 01. 同构结构化并发：集合遍历（`for_each`）

当需要对图中的某个**非空强类型列表（`type: array`）**或**非空强类型有序映射（`type: object` + `additionalProperties`）**逐项执行同构任务时，可以在任意节点（`run`、`use` 或内联 `graph`）上挂载 **`for_each`** 修饰符。

`for_each` 为每次迭代创建独立的局部帧，在内部消费单次迭代的 `branches`（与 `catch`），对外层图的 `edges` 始终呈现为**单一无分支出边（`node -> target`）**，并在输出端提供三类正交数据通道：**`filter`**、**`collect`** 与 **`state`**。

---

## 一、 单次迭代局部帧注入（`source -> item / key / index`）

1. **遍历列表（`type: array`）**：
   * `source`：绑定已定值的非空列表变量（或结构体字段路径如 `batch.items`）；
   * `item`（必填）：拆包注入当前元素的局部变量名；
   * `index`（可选）：注入当前下标 `0, 1, ...`（类型 `integer`）；此时禁止声明 `key`。
2. **遍历有序映射（`type: object` + `additionalProperties`）**：
   * 按 Ordered Map 插入顺序遍历；
   * `item`（必填）：注入当前值（Value）；
   * `key`（可选）：注入当前键（Key，类型 `string`）；
   * `index`（可选）：注入有序序号 `0, 1, ...`（类型 `integer`）。
3. **`concurrency`（可选）**：最大并发执行度（正整数，默认 `1`；当未声明 `state` 时可设为 `> 1` 开启并发遍历）。

---

## 二、 三类正交输出通道（`filter` / `collect` / `state`）

### 1. `filter`：按迭代分支零拷贝保留原元素（`Array<T> -> Array<T>`）
当节点声明了 `branches`（或 `catch`）时，`filter: { <branch>: <target_var> }` 会在单次迭代走入 `<branch>` 分支时，直接将该次迭代的**原始输入元素 `item`**（若为有序映射则保留原键值对 `key: item`）零拷贝收入外层变量 `<target_var>` 中：

```yaml
triage_articles:
  for_each:
    source: feed_payload.articles
    item: current_article
    concurrency: 5
    filter:
      keep: valid_articles      # 自动推导为与 source 相同的列表类型
      flagged: review_queue
  input: [current_article]
  branches:
    keep: "符合规范，保留"
    flagged: "疑似违规，转人工复核"
    drop: "垃圾内容，直接丢弃"
  run: "审阅文章 variables.current_article.title，选择 keep、flagged 或 drop 分支。"
```

### 2. `collect`：收集节点单次产出并提升为集合（`Array<T> -> Array<U>`）
节点单次迭代产出的字段 `<single_field>`（非 `state` 字段）仅活在单次迭代帧内，通过 `collect` 收集提升为外层集合变量 `<target_var>`：
* **默认提升（`collect: { <single_field>: <target_var> }`）**：自动**只从所选分支产出了该字段的迭代中收集非空结果**（源为列表时提升为 `Array<U>`，源为有序映射时保留原键提升为 `OrderedMap<string, U>`）。
* **单层扁平化提升（`collect: { <single_field>: { into: <target_var>, flatten: true } }`）**：当源为列表且单次产出 `<single_field>` 本身也是列表 `Array<U>` 时，展开拼接为一维列表 `Array<U>`。

```yaml
analyze_articles:
  for_each:
    source: valid_articles
    item: current_article
    index: idx
    concurrency: 5
    collect:
      score: all_scores
      featured_summary: featured_summaries   # 仅收集走 high_quality 分支产出的非空 string
      article_tags:
        into: all_tags
        flatten: true                        # 将每篇产出的标签列表拼接为一维总列表
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

### 3. `state`：跨元素有状态串行累积折叠（`Sequential Fold`）
当第 $i$ 次迭代需要读取并更新第 $i-1$ 次迭代累积下来的状态变量时，声明 `state: [<state_var>, ...]`（此时 **`concurrency` 必须为 `1`**）：

```yaml
read_chapters_sequentially:
  for_each:
    source: book_data.chapters
    item: current_chapter
    index: chapter_idx
    concurrency: 1
    state: [rolling_summary]      # 跨元素原地演进外层已定值的 rolling_summary
    collect:
      single_note: chapter_notes  # 同时可配合 collect 收集每章札记
  input: [chapter_idx, current_chapter, rolling_summary]
  output:
    rolling_summary: { type: string }
    single_note: { type: string }
  run: "阅读第 variables.chapter_idx 章 variables.current_chapter，结合上一章累积摘要 variables.rolling_summary 更新写入 output.rolling_summary，并撰写本章札记写入 output.single_note。"
```

### 4. 遍历强类型有序映射（Ordered Mapping）

```yaml
translate_sections:
  for_each:
    source: section_map           # 类型为 { type: object, additionalProperties: { type: string } }
    key: section_id               # 注入当前键 (string)
    item: section_body            # 注入当前值 (string)
    concurrency: 4
    collect:
      translated_body: translated_section_map # 自动推导为 { type: object, additionalProperties: { type: string } }
  input: [section_id, section_body]
  output:
    translated_body: { type: string }
  run: "翻译章节 variables.section_id 的内容 variables.section_body，写入 output.translated_body。"
```

> 更多高阶组合模板请参见 Cookbook：
> * [有状态串行折叠](../../cookbook/02-collection-and-concurrency/01-stateful-sequential-fold.md)
> * [并发多路分流与扁平化](../../cookbook/02-collection-and-concurrency/02-multi-way-triage-and-flatmap.md)
> * [外层 `for_each` 并发遍历 + 内层 SESE 子图 `while` 循环](../../cookbook/02-collection-and-concurrency/03-concurrent-map-with-inner-loop.md)

---

`for_each` 处理的是同质集合的并发遍历。下一节介绍如何同时并发启动多个职责不同的异构节点：[02. 异构结构化并发：Fork-Join 并发组（`-> [a, b] ->`）](./02-fork-join.md)。
