# 01. 同构结构化并发：集合遍历（`for_each`）

当需要对某个非空强类型列表（`type: array`）或非空强类型有序映射（`type: object` + `additionalProperties`）中的每个元素执行无跨元素依赖的同构处理时，可在任意节点（`run`、`use: workflow:<PackagePath>` 或内联 `graph`）上挂载 **`for_each`** 修饰符。

`for_each` 是纯粹的**输入集合切片与并发窗口修饰符**，其核心心智模型包含以下四点：

* **输入集合切片与局部隔离**：为每次元素迭代创建独立的局部隔离帧（`Iteration Frame`），将当前元素、键与序号注入局部作用域；
* **并发窗口控制**：通过 `concurrency` 限制同时激活的最大迭代数，并在全部迭代完成后执行保序屏障汇聚；
* **内部消化分支与异常**：在单次迭代内部消化节点的 `branches` 与 `catch`，对外层图的 `edges` 始终呈现为**单条无分支出边（`node -> target`）**；
* **一阶同构集合提升**：直接复用节点自身的 `output`、`with.output` 与 `branches.<branch>.output` 完成按分支过滤与集合提升（目标集合变量须已在所属图的宿主 `output`、未挂载 `for_each` 节点的 `output` 或 `graph.variables` 中显式声明 `TypeSpec`）。

---

## 一、 单次迭代局部帧注入与并发控制（`source -> item / key / index / concurrency`）

`for_each` 仅包含 5 个输入切片与并发调度属性（`source`、`item`、`key`、`index`、`concurrency`），支持遍历列表与有序映射两类集合，并通过 `concurrency` 控制最大并发激活迭代数（正整数，默认 `1`；可设为 `> 1` 开启并发遍历）。

> [!NOTE]
> **三类节点挂载 `for_each` 时的输入与输出映射规则**：
> * **`run`（`ActionNode`）**：节点 `input: [...]` **必须显式列出** `run` 所需读取的 `item` / `key` / `index` 及外层变量（未列入 `input` 的变量不会进入传给业务执行器的 `input_snapshot` 及 `builtin` 的 `Input` 模型）；`output` 声明单次产出类型，在外层自动提升为对应集合。
> * **`use`（`CallNode`）**：在 `with.input` 中直接引用 `item` / `key` / `index`（或其结构体字段路径）及外层变量；在 `with.output` 中将被调用方单次产出的字段名映射至外层目标集合变量名，在外层自动提升为列表或有序映射。
> * **内联 `graph`（`InlineGraphNode`）**：由于 `item` / `key` / `index` 已自动注入子图的初始 `Block Frame`，内联 `graph` 节点的 `input: [...]` 仅用于捕获外层普通变量，**禁止**重复列出 `item` / `key` / `index`。

### 1. 遍历强类型列表（`type: array`）

* **`source`（必填）**：绑定已定值的非空列表变量或结构体字段路径（如 `batch.items`）。
* **`item`（必填）**：注入当前迭代帧的元素变量名（类型自动推导为列表 `items` 类型）。
* **`index`（可选）**：注入当前下标 `0, 1, ...`（类型 `integer`）；遍历列表时禁止声明 `key`。
* **一阶类型提升（`U -> Array<U>`）**：节点 `output`（或 `with.output`）映射的单次产出类型 $U$，在外层图符号表中自动对应为列表类型 `{ type: array, items: U }`。

```yaml
# 示例 A：在 run 节点上挂载 for_each 遍历列表
process_list_items:
  for_each:
    source: batch.items      # 源列表：{ type: array, items: { type: string } }
    item: current_item       # 注入当前元素（类型自动推导为 string）
    index: item_idx          # 注入当前下标 0, 1, ...（类型为 integer）
    concurrency: 4           # 最多同时并发执行 4 个元素迭代
  input: [item_idx, current_item]
  # output 声明单次元素类型 string；在外层图中 results 对应为 Array<string>
  output:
    results: { type: string }
  run:
    executor: builtin
    content: |
      return Output(results=f"[{input.item_idx}] {input.current_item}")

# 示例 B：在 use: workflow:<PackagePath> 节点上挂载 for_each 遍历列表
audit_sub_items:
  for_each:
    source: batch.items
    item: current_item
    concurrency: 4
  use: workflow:audit.single_item_audit
  with:
    input:
      raw_text: current_item
    # 将子工作流单次输出字段 report 映射至外层列表变量 audit_reports（Array<Report>）
    output:
      report: audit_reports
```

### 2. 遍历强类型有序映射（`type: object` + `additionalProperties`）

* **`source`（必填）**：绑定已定值的非空有序映射变量或结构体字段路径，按 Ordered Map 插入顺序遍历。
* **`item`（必填）**：注入当前值（Value，类型自动推导为 `additionalProperties` 类型）。
* **`key`（可选）**：注入当前键（Key，类型 `string`）。
* **`index`（可选）**：注入当前序号 `0, 1, ...`（类型 `integer`）。
* **一阶类型提升（`U -> OrderedMap<string, U>`）**：节点 `output`（或 `with.output`）映射的单次产出类型 $U$，在外层图符号表中自动保留原键序对应为有序映射类型 `{ type: object, additionalProperties: U }`。

```yaml
translate_sections:
  for_each:
    source: section_map      # 源有序映射：{ type: object, additionalProperties: { type: string } }
    key: section_id          # 注入当前键（类型为 string）
    item: section_body       # 注入当前值（类型为 string）
    index: section_idx       # 注入当前序号（类型为 integer）
    concurrency: 4
  input: [section_id, section_body]
  # 单次产出 string，在外层图中保留原键 section_id 提升为 OrderedMap<string, string>
  output:
    translated_section_map: { type: string }
  run:
    executor: builtin
    content: |
      return Output(translated_section_map=f"[{input.section_id}] {input.section_body}")
```

---

## 二、 统一输出契约：一阶提升、多路分流、零拷贝透传与二维展平

挂载 `for_each` 的节点不引入任何额外的输出绑定语法，而是与普通节点完全共享 `output`、`with.output` 与 `branches.<branch>.output`。

### 1. 利用 `branches.<branch>.output` 实现多路分流与条件过滤

当挂载 `for_each` 的节点声明了 `branches`（或 `catch`）时，各正常分支通过 `branches.<branch>.output: [<var_1>, ...]` 声明当次迭代走该分支时产出的字段子集：

* 在屏障保序汇聚时，对于每个外层目标变量 `<var>`，运行时按源集合顺序 `0 ... n-1` **仅收集实际产出了 `<var>` 的迭代结果**；
* 走未包含 `<var>` 的分支（如 `output: []`）或被 `catch` 捕获的异常迭代会自动跳过，从而自然实现条件过滤（Filter）与多路分流（Partition）：

```yaml
analyze_feedbacks:
  for_each:
    source: batch.feedbacks
    item: fb
    concurrency: 4
  input: [fb]
  # 外层图中 urgent_tickets 对应为 Array<Ticket>，normal_summaries 对应为 Array<string>
  output:
    urgent_tickets:
      type: object
      properties:
        id: { type: string }
        reason: { type: string }
      required: [id, reason]
    normal_summaries: { type: string }
  branches:
    urgent:
      description: "紧急故障，收集至 urgent_tickets"
      output: [urgent_tickets]
    normal:
      description: "普通反馈，收集至 normal_summaries"
      output: [normal_summaries]
    spam:
      description: "垃圾信息，不产出任何字段（在汇聚时自动跳过）"
      output: []
  run:
    executor: builtin
    content: |
      if "spam" in input.fb:
          return Branch.spam()
      if "URGENT" in input.fb:
          return Branch.urgent(
              urgent_tickets=Output_urgent_tickets(id="T-1", reason=input.fb)
          )
      return Branch.normal(normal_summaries=input.fb)
```

### 2. 利用内联 `graph` + `builtin:assign` 实现原元素零拷贝过滤与死信分流

当需要在不让执行器重抄输入元素的前提下，按分支或异常原样保留输入元素 `item`（例如将合格元素过滤至 `valid_items`、将异常元素收集至死信队列 `failed_items`）时，可在 `for_each` 挂载的内联 `graph` 分支中调用零开销算子 [`builtin:assign`](../../reference/builtins/assign.md) 将 `item` 直接赋给目标输出变量：

```yaml
filter_and_process_items:
  for_each:
    source: raw_items        # 源列表：{ type: array, items: { type: string } }
    item: current_item
    concurrency: 8
  output:
    valid_items: { type: string }
    failed_items: { type: string }
  branches:
    kept:
      description: "保留合格条目"
      output: [valid_items]
    dropped:
      description: "丢弃不合格条目"
      output: []
    errored:
      description: "处理异常进入死信队列"
      output: [failed_items]
  graph:
    nodes:
      judge:
        input: [current_item]
        branches:
          keep: "质量合格"
          drop: "质量不合格"
        catch:
          err: ["*"]
        run:
          executor: builtin
          content: |
            if len(input.current_item.strip()) >= 3:
                return Branch.keep()
            return Branch.drop()

      # 在 keep 分支上通过 builtin:assign 零拷贝透传原元素 current_item
      assign_valid:
        use: builtin:assign
        with:
          input: { source: current_item }
          output: { value: valid_items }

      # 在异常捕获分支上通过 builtin:assign 零拷贝将原元素归入死信队列
      assign_failed:
        use: builtin:assign
        with:
          input: { source: current_item }
          output: { value: failed_items }

    edges:
      - start -> judge
      - judge.keep -> assign_valid
      - assign_valid -> end.kept
      - judge.drop -> end.dropped
      - judge.err -> assign_failed
      - assign_failed -> end.errored
```

### 3. 利用 [`builtin:array_flatten`](../../reference/builtins/array-flatten.md) 展平二维列表

当单次迭代产出的字段本身是一个列表 `Array<W>` 时，`for_each` 遵循纯粹的一阶同构提升规则将其汇聚为二维列表 `Array<Array<W>>`；随后串联确定性内建算子 [`builtin:array_flatten`](../../reference/builtins/array-flatten.md) 即可将其保序展平为一维列表 `Array<W>`：

```yaml
# 步骤 1：单次迭代产出 Array<string>，经 for_each 一阶提升为二维列表 Array<Array<string>>
extract_tags_per_article:
  for_each:
    source: articles
    item: article
    concurrency: 4
  input: [article]
  output:
    nested_tags:
      type: array
      items: { type: string }
  run:
    executor: builtin
    content: |
      return Output(nested_tags=input.article.title.lower().split())

# 步骤 2：调用 builtin:array_flatten 将二维列表 nested_tags 展平为一维列表 all_tags
flatten_all_tags:
  use: builtin:array_flatten
  with:
    input: { source: nested_tags }
    output: { result: all_tags }
```

> [!TIP]
> **跨元素有状态串行折叠与条件早退**：
> `for_each` 专职处理无跨元素依赖的同构并行转换与分流。若业务需要**第 $i$ 步读取并更新第 $i-1$ 步累积状态**的有状态串行折叠（Sequential Fold），或需在遍历途中满足条件即提前退出（Short-Circuit / Break），请使用 [成环回边与队列消费算子（`builtin:assign` + `builtin:array_shift` + `builtin:array_append`）](../02-control-flow/02-cycles-and-back-edges.md)。

---

## 三、 完整示例：并发分流过滤、结果收集与二维标签展平流水线

以下完整工作流串联两个 `for_each` 节点与一个 `builtin:array_flatten` 节点：第一步通过内联 `graph` + `builtin:assign` 对原始文稿列表执行并发三路零拷贝分流，第二步对保留下来的合规文稿并发提取精选摘要与每篇标签子列表，第三步将二维标签列表展平为一维标签总表：

```yaml
# 1. 根节点输入与输出契约
input:
  articles:
    type: array
    items: { type: string }

output:
  review_queue:
    type: array
    items: { type: string }
  featured_summaries:
    type: array
    items: { type: string }
  all_tags:
    type: array
    items: { type: string }

graph:
  # 2. 显式声明由 for_each 写入的局部中间集合变量 valid_articles 与 nested_tags
  # （review_queue、featured_summaries、all_tags 已在顶层 output 中声明）
  variables:
    valid_articles:
      type: array
      items: { type: string }
    nested_tags:
      type: array
      items:
        type: array
        items: { type: string }

  nodes:
    # 阶段 1：并发遍历原始 articles，通过内联 graph + builtin:assign 零拷贝分流原元素
    # 注意：keep / flagged / drop 分支在 for_each 内部消费，外层 edges 仅写单条无分支出边
    triage_articles:
      for_each:
        source: articles
        item: current_article
        concurrency: 5
      output:
        valid_articles: { type: string }
        review_queue: { type: string }
      branches:
        keep:
          description: "内容合规，进入深度分析队列 valid_articles"
          output: [valid_articles]
        flagged:
          description: "疑似违规，转入人工复核队列 review_queue"
          output: [review_queue]
        drop:
          description: "无效内容，直接丢弃"
          output: []
      graph:
        nodes:
          audit_single:
            input: [current_article]
            branches:
              keep: "内容合规"
              flagged: "疑似违规"
              drop: "无效垃圾内容"
            run:
              executor: builtin
              content: |
                text = input.current_article.strip()
                if not text:
                    return Branch.drop()
                if "FLAG" in text:
                    return Branch.flagged()
                return Branch.keep()

          keep_article:
            use: builtin:assign
            with:
              input: { source: current_article }
              output: { value: valid_articles }

          flag_article:
            use: builtin:assign
            with:
              input: { source: current_article }
              output: { value: review_queue }

        edges:
          - start -> audit_single
          - audit_single.keep -> keep_article
          - keep_article -> end.keep
          - audit_single.flagged -> flag_article
          - flag_article -> end.flagged
          - audit_single.drop -> end.drop

    # 阶段 2：并发遍历 valid_articles，按分支产出 featured_summaries 与二维标签列表 nested_tags
    analyze_valid_articles:
      for_each:
        source: valid_articles
        item: current_article
        index: idx
        concurrency: 5
      input: [idx, current_article]
      output:
        featured_summaries: { type: string }
        nested_tags:
          type: array
          items: { type: string }
      branches:
        high_quality:
          description: "优质文稿，产出精选摘要与标签"
          output: [featured_summaries, nested_tags]
        standard:
          description: "常规文稿，仅产出标签"
          output: [nested_tags]
      run:
        executor: builtin
        content: |
          tags = [w.lower() for w in input.current_article.split() if len(w) > 3]
          if len(input.current_article) >= 20:
              return Branch.high_quality(
                  featured_summaries=f"#{input.idx}: {input.current_article[:20]}",
                  nested_tags=tags,
              )
          return Branch.standard(nested_tags=tags)

    # 阶段 3：使用 builtin:array_flatten 将二维标签列表 nested_tags 保序展平为一维列表 all_tags
    flatten_tags:
      use: builtin:array_flatten
      with:
        input: { source: nested_tags }
        output: { result: all_tags }

  # 3. 外层控制流边：挂载了 for_each 的节点对外均为单一无分支出边
  edges:
    - start -> triage_articles
    - triage_articles -> analyze_valid_articles
    - analyze_valid_articles -> flatten_tags
    - flatten_tags -> end
```

> 更多集合与并发组合模式可参阅 Cookbook：
> * [有状态串行折叠](../../cookbook/02-collection-and-concurrency/01-stateful-sequential-fold.md)
> * [并发多路分流与扁平化](../../cookbook/02-collection-and-concurrency/02-multi-way-triage-and-flatmap.md)
> * [外层 `for_each` 并发遍历 + 内层 SESE 子图循环](../../cookbook/02-collection-and-concurrency/03-concurrent-map-with-inner-loop.md)

---

下一节：[02. 异构结构化并发：Fork-Join 并发组（`-> [a, b] ->`）](./02-fork-join.md)。
