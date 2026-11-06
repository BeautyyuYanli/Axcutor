# 嵌套遍历与条件循环组合模式（`for_each` + SESE 内联 `graph`）

在“万物皆 Node，`run`、`graph`、`use` 作为节点的三种平等执行体”架构下，内联 `graph` 节点作为唯一的**单入口单出口（Single-Entry-Single-Exit, SESE）块级组合容器**。将 `for_each` 直接挂载在内联 `graph` 节点上（或将内联 `graph` 放入结构化并发组 `[worker_a, worker_b]` 中），即可在单个工作流文件内正交组合**“外层并发/集合遍历 + 内层多步串联、条件分支、异常恢复或独立 `while` 循环”**。

---

## 一、 内联 `graph` 的块级作用域（Block Frame）、显式捕获与迭代分支消费

当 `graph` 作为图内节点内联声明时，它对应受控的**显式捕获块级作用域（Block Scope）**，无需像跨文件 `use` 那样书写 `with` 映射或重复声明输入/输出变量的 `TypeSpec`：

1. **显式输入捕获（`input: [<var>, ...]`，杜绝未定值变量穿透）**：
   * 内联 `graph` 节点通过轻量级顶层变量名列表 **`input: [<var_1>, ...]`** 显式声明需要从外层图捕获的顶层变量（未列入 `input` 的外层变量对子图完全不可见）。
   * 静态检查器在**外层图到达该内联 `graph` 节点的入口处**直接校验 `input` 列表中的每个变量是否均在外层已定值（从而在编译期 100% 拦截读取外层下游节点变量或未初始化变量的穿透行为）。
   * 运行时启动内联子图（或 `for_each` 的每一次迭代子图）时，直接以**该节点 `input` 捕获的外层变量只读快照 + `for_each` 注入的 `item`/`key`/`index`（若有）+ `for_each.state` 累积变量（若有）+ 子图自身的 `graph.variables`** 初始化子图栈帧的独立 `variables` 表。
2. **输出列表简写（`output: [<var_1>, ...]`，消除跨层重复类型声明）**：
   * 由于子图内部节点（或 `graph.variables`）已经声明了变量的完整 `TypeSpec`，内联 `graph` 节点对外可直接声明**字段名列表简写 `output: [chapter_translation]`**，由静态检查器自动从子图符号表提取其 `TypeSpec`（无环内联子图亦无需配置 `max_steps`）。
3. **宿主输入与 `graph.variables` 初值互斥（消除覆盖歧义）**：
   * 凡已列入内联 `graph` 节点 `input: [...]` 捕获列表的变量（以及 `for_each` 注入的 `item`/`key`/`index` 与 `for_each.state` 变量），在进入子图时已拥有确定的外层运行期值，**严禁在子图 `graph.variables` 中重复声明同名初值**。
   * 仅当子图对外导出的 `output` 变量 `X` **未列入**内联 `graph` 节点的 `input` / `for_each.state`（即子图不读取外层 `X` 的旧值，或 `X` 是由该子图首次产出给外层的变量）时，子图才允许在自身的 `graph.variables` 中为 `X` 声明初值。
4. **局部状态封装与同帧命名无冲突（Scoped Encapsulation & Non-Collision）**：
   * 子图内部节点的 `output` 仅修改子图自己栈帧中的 `variables`。在并发 `for_each` 或并发组 `[worker_a, worker_b]` 下，各分支的子图栈帧完全隔离，互不干扰。
   * 由于未列入内联 `graph` 节点 `input: [...]` 的外层变量对子图完全不可见，**子图的私有变量（如内层循环独立的 `retry_count` 等）允许与外层图中未捕获的变量同名**，保障块级作用域的模块化封装。
   * 当内联 `graph` 挂载 `for_each` 时，注入的 `item`、`key`、`index` 之间不可互相同名，且**不可与该内联 `graph` 节点 `input: [...]` 捕获的变量、`for_each.state` 变量或对外单次 `output` 字段重名**。
5. **迭代分支消费与无 `null` 条件收集**：
   * 当内联 `graph` 挂载 `for_each` 时，子图可声明迭代级 `branches`（如 `pass` 与 `drop`）以及 `catch` 异常捕获分支，并在子图内部通过 `end.pass`（关联 `output: [chapter_translation]`）与 `end.drop`（关联 `output: []`）结束单次迭代（或发生异常时由 `catch` 拦截并等价于 `output: []`）。
   * `for_each.collect` 自动只从走 `end.pass` 产出了 `chapter_translation` 的迭代中收集纯正非空译文，走 `end.drop` 或 `catch` 的章节自动跳过（同时可通过 `for_each.filter` 零拷贝保留被放弃或异常章节的原始结构体），无需在子图 `graph.variables` 中预置 `null` 或将类型降级为 `[string, "null"]`。

---

## 二、 完整示例：批量并发翻译章节 + 单章内部 `while` 循环审校（含显式 `input` 捕获、`output` 列表简写、迭代分支淘汰 `null` 与原章节零拷贝归档）

```yaml
input:
  target_language:
    type: string
    description: "目标翻译语言（如中文、英文）"
  chapters:
    type: array
    items:
      type: object
      properties:
        title: { type: string }
        body: { type: string }
      required: [title, body]
    description: "原始章节结构体列表"
output:
  translated_chapters:
    type: array
    items:
      type: string
    description: "全部审校通过的译文章节列表（纯正非空 string）"
  dropped_chapters:
    type: array
    items:
      type: object
      properties:
        title: { type: string }
        body: { type: string }
      required: [title, body]
    description: "审校重试耗尽后被放弃的原始章节结构体列表（由 filter 零拷贝保留）"

graph:
  nodes:
    # 在内联 graph 节点上直接挂载 for_each，并通过 input 显式捕获外层已定值的 target_language
    batch_translate:
      for_each:
        source: chapters         # 遍历外层 variables.chapters
        item: single_chapter     # 拆包注入到每次迭代子图 variables 中的单章结构体 (TypedDict: { title, body })
        concurrency: 3
        # 1. 零拷贝保留走 drop 分支的原始章节结构体，供后续人工处理
        filter:
          drop: dropped_chapters
        # 2. 自动仅收集走 pass 分支产出的非空 chapter_translation (string)
        collect:
          chapter_translation: translated_chapters
      input: [target_language]
      # 内联子图输出列表简写：直接从子图符号表自动推导 chapter_translation 的 TypeSpec（纯正非空 string）
      output: [chapter_translation]
      branches:
        pass:
          description: "单章审校通过，导出译文"
          output: [chapter_translation]
        drop:
          description: "单章重试耗尽放弃，不导出译文"
          output: []

      # 内联展开单章的 while 循环控制流图（SESE 块级子栈帧）
      graph:
        max_steps: 20
        # 每次迭代独立初始化的子图私有状态（多章并发时完全隔离，无需预置 chapter_translation: null 假初值）
        variables:
          revision_notes: ""
          retry_count: 0

        nodes:
          translate:
            input: [target_language, single_chapter, revision_notes, retry_count]
            output:
              draft_translation: { type: string }
              retry_count: { type: integer }
            run: "将章节《variables.single_chapter.title》的正文 variables.single_chapter.body 翻译为 variables.target_language（若有 variables.revision_notes 则按意见修正），写入 output.draft_translation，并将 variables.retry_count 加一后写入 output.retry_count。"

          proofread:
            input: [draft_translation, retry_count]
            output:
              revision_notes: { type: string }
              chapter_translation: { type: string }
            branches:
              pass:
                description: "译文信达雅，直接通过"
                output: [chapter_translation]
              rework:
                description: "译文有瑕疵且未满 2 次重试，打回重译"
                output: [revision_notes]
              drop:
                description: "已满 2 次重试仍不合格，放弃该章"
                output: []
            run:
              - "审校译文 variables.draft_translation。"
              - "若合格，将 variables.draft_translation 写入 output.chapter_translation，选 pass。"
              - "若不合格且 variables.retry_count < 2，将修改意见写入 output.revision_notes，选 rework。"
              - "若不合格且 variables.retry_count >= 2，直接选 drop。"

        edges:
          - start -> translate
          - translate -> proofread
          - proofread.rework -> translate  # 单章内部的 while 循环回边
          - proofread.pass -> end.pass     # 审校通过，导出非空 chapter_translation 供外层 collect 收集
          - proofread.drop -> end.drop     # 放弃该章，不导出译文（同时触发外层 filter.drop 零拷贝归档原章节）

  edges:
    - start -> batch_translate
    - batch_translate -> end
```

---

## 三、 图内三种节点形态的边界对比

| 图内节点形态 | 输入输出与容错声明方式 | 栈帧与变量规则 |
| :--- | :--- | :--- |
| **1. 内联动作 (`run`)** | 声明 `input: [<name>, ...]`（可选，默认 `[]`）、`output?`、`branches?`、`for_each?`、`timeout_ms?`、`retry?`、`catch?`，无需 `with` | 仅只读访问 `input` 列表中显式声明的当前图 `variables`（及 `for_each` 注入项，支持 `variables.<name>.<field>` 静态字段投影），按所选正常分支契约通过 `output`（及 `for_each.filter` / `for_each.collect` / `for_each.state`）提交顶层变量更新；发生异常进入 `catch` 分支时不提交任何 `output`。 |
| **2. 内联子图 (`graph`)** | 声明 `input: [<name>, ...]`（可选，默认 `[]`）、`output?`（支持 `[<name>, ...]` 简写）、`branches?`、`for_each?`、`timeout_ms?`、`retry?`、`catch?`，无需 `with` | **块级子栈帧（Block Frame，唯一 SESE 组合容器）**：仅以 `input` 显式捕获的外层 `variables` 快照 + `for_each` 注入项 + `for_each.state` 项 + 子图 `graph.variables` 初始化独立变量表（宿主输入严禁在子图 `graph.variables` 重复声明，子图私有变量与未捕获外层变量完全隔离）；子图到达 `end`（或 `end.<choice>`）时按分支关联的 `output` 提取变量提交回外层图；子图到达 `fail` 或抛出未捕获异常时自动回滚丢弃整个子栈帧状态。 |
| **3. 外部调用 (`use`)** | 契约来自目标文件或 `builtin:*`，可选配 `with`、`for_each` 以及（仅限 `workflow:<path>`）`timeout_ms?`、`retry?`、`catch?` | **函数调用栈帧（Function Frame）**：完全不继承外层 `variables`，统一通过 `with.input`（支持顶层变量、非空 `TypedDict` 静态字段路径投影及常量字面量）与 `with.output` 适配输入输出；挂载 `for_each` 时，透明衔接 `for_each.filter`、`for_each.collect` 或 `for_each.state` 写回外层图。 |
