# RFC-0008: 移除 `for_each.collect` 与 `for_each.filter`，统一复用节点原生 `output` / `with.output` 与分支子集契约

* **状态**：Proposed
* **所属分类**：核心原语正交化与规范简化（Language Orthogonality & Specification Simplification）
* **前置关联提案**：
  * [`RFC-0006: 移除 for_each.state，解耦无状态同构并行遍历与有状态串行循环`](./0006-remove-for-each-state.md)（方案 A1）
  * [`RFC-0007: 移除四项隐式类型推导与绑定语法糖，收敛为单遍显式符号表与模块化类型检查`](./0007-explicit-symbol-declarations-and-type-checking-simplification.md)
* **核心解决的问题**：
  1. 在 [`RFC-0006`](./0006-remove-for-each-state.md) 移除 `for_each.state` 的基础上，进一步从 `ForEachSpec` 中彻底移除 `collect`（含内嵌 `flatten`）与 `filter`，使 `for_each` 收敛为纯粹的**输入集合切片与并发窗口修饰符**（仅含 `source`、`item`、`key?`、`index?`、`concurrency?`）；
  2. 将 `for_each` 的输出收集与按分支过滤完全统一至节点原生的 `output`（对 `ActionNode` / `InlineGraphNode`）、`with.output`（对 `CallNode`）以及 `branches.<branch>.output` 分支输出子集机制，消除“两套输出绑定与重命名语法”引发的 `CallNode` 禁用 `with.output` 特判及单次输出防遮蔽规则；
  3. 将原内嵌于 `collect.<field>.flatten: true` 的二维列表展平能力剥离为独立的正交确定性算子 **`builtin:array_flatten`**，使 `for_each` 永远保持纯粹的一阶同构集合类型提升（`T -> Array<T>` 或 `OrderedMap<string, T>`）。
* **关联规范模块（`manual/spec/`）**：
  * [`01-grammar-and-ast/01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md)
  * [`01-grammar-and-ast/02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md)
  * [`02-type-system-and-symbols/03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md)
  * [`02-type-system-and-symbols/05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md)
  * [`03-static-flow-analysis/02-scoping-and-collision-rules.md`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md)
  * [`03-static-flow-analysis/03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)
  * [`03-static-flow-analysis/04-concurrent-disjoint-write-analysis.md`](../manual/spec/03-static-flow-analysis/04-concurrent-disjoint-write-analysis.md)
  * [`04-runtime-execution-model/01-frame-memory-model.md`](../manual/spec/04-runtime-execution-model/01-frame-memory-model.md)
  * [`04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md`](../manual/spec/04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md)
* **关联文档模块（`manual/docs/`）**：
  * [`guide/03-concurrency/01-for-each.md`](../manual/docs/guide/03-concurrency/01-for-each.md)
  * [`guide/01-foundations/02-use-subworkflows.md`](../manual/docs/guide/01-foundations/02-use-subworkflows.md)
  * [`guide/04-state-and-resilience/01-frames-and-scoping.md`](../manual/docs/guide/04-state-and-resilience/01-frames-and-scoping.md)
  * [`guide/index.md`](../manual/docs/guide/index.md)
  * [`cookbook/02-collection-and-concurrency/02-map-filter-and-dlq.md`](../manual/docs/cookbook/02-collection-and-concurrency/02-map-filter-and-dlq.md)
  * [`reference/01-node-and-graph-fields.md`](../manual/docs/reference/01-node-and-graph-fields.md)
  * [`reference/builtins/index.md`](../manual/docs/reference/builtins/index.md)

---

## 一、 问题背景与架构耦合分析

在 [`RFC-0006`](./0006-remove-for-each-state.md) 剥离了 `for_each.state` 之后，`ForEachSpec` 仍然残留了 `collect` 与 `filter` 两个输出汇聚配置项。深入审视可以发现，这两个配置项与节点原生的输出及分支契约存在严重的概念重叠与语法冲突。

---

### 痛点 8.1：`for_each.collect` 与节点原生 `output` / `with.output` 职责重复，造成语法特判与双重命名负担

在 Axcutor 中，普通节点已经具备完备的输出契约语法：
* `ActionNode`（`run`）与 `InlineGraphNode`（`graph`）通过 `output: Map<Identifier, TypeSpec>` 声明写入外层图变量的字段名与类型；
* `CallNode`（`use: workflow:<path>`）通过 `with.output: Map<Identifier, Identifier>` 将被调用子工作流的输出字段重映射至外层图变量。

然而，当节点挂载 `for_each` 时，现行规范强制引入了第二套输出绑定表 `for_each.collect: { <single_field>: <target_var> }`，引发了三处连锁畸变：
1. **强行禁用 `CallNode` 的 `with.output`**：
   * [`01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md#L33) 被迫规定：“当 `CallNode` 声明了 `for_each` 时，禁止在 `with` 中声明 `output`”。同一类 `use: workflow:<path>` 节点，仅仅因为是否挂载 `for_each`，其输出映射位置就在 `with.output` 与 `for_each.collect` 之间发生割裂。
2. **强制发明两套名字与防遮蔽特判**：
   * 对于 `ActionNode` 与 `InlineGraphNode`，作者被迫为每一个输出数据发明两个不同的名字：先在 `node.output` 中起一个“单次迭代局部字段名 `<single_field>`”，再在 `for_each.collect` 中将其映射为“外层图集合变量名 `<target_var>`”；
   * 为防止这两套名字混淆，[`02-scoping-and-collision-rules.md`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md#L40-L41) 不得不专门增加第 4 条公理：“单次迭代产出的局部字段名 `<single_field>` 禁止与外层图符号表 $\Sigma_{\text{outer}}$ 中的变量同名（包含不可与 `collect` 映射的目标集合变量 `<target_var>` 同名）”。
3. **破坏节点输出接口的统一性**：
   * 在静态流分析（[`03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md) 与 [`04-concurrent-disjoint-write-analysis.md`](../manual/spec/03-static-flow-analysis/04-concurrent-disjoint-write-analysis.md)）中，计算节点的写入集合 $OutVars(N)$ 无法统一读取 `N.output` / `N.with.output`，必须处处对“未挂载 `for_each`”与“挂载了 `for_each`”做分支特判。

---

### 痛点 8.2：`branches.<branch>.output` 已经原生支持按分支过滤与分流，`for_each.filter` 造成概念冗余

Axcutor 的 `branches` 契约原生支持为不同分支指定不同的输出字段子集（`branches.<branch>.output: [<field_1>, ...]`）：
* 当某次迭代走分支 $b$ 时，仅产出 `branches[b].output` 中声明的字段；
* 当某次迭代走不产出任何字段的分支（`output: []`）或被 `catch` 捕获时，不产出任何字段。

这意味着，**“按分支过滤（Filter）与多路分流（Partition）”本就是 `branches.<branch>.output` 在集合提升下的自然推论**：外层目标集合只需按序收集实际产出了该字段的迭代结果即可。

而现行规范之所以在 `collect` 之外又单独设计了 `for_each.filter: Map<BranchOrCatchId, Identifier>`，仅仅是为了在单节点 `run` 中“不经过 `output` 直接从输入端零拷贝捞取原元素 `item`”。为了这一个特殊捷径，`ForEachSpec` 被迫将“控制流分支名（`BranchOrCatchId`）”与“输入端源元素（`item`）”跨端耦合进输出绑定通道中，并引入了 `V_filter ∩ V_collect = ∅` 等跨通道互斥检查。事实上，若用户需要零拷贝透传庞大的原始输入元素 `item`（避免 LLM 在 JSON 输出中重抄原文），只需在 `for_each` 挂载的内联 `graph` 分支中使用确定性算子 `builtin:assign` 将 `item` 赋给输出变量即可，完全不需要在 `for_each` 语法层专设 `filter` 通道。

---

### 痛点 8.3：`collect.<field>.flatten: true` 将集合变换算子耦合进节点修饰符

现行规范允许在 `collect` 中书写 `{ into: <target_var>, flatten: true }`，用于在汇聚时顺带将单次迭代产出的 `Array<W>` 展平为一维 `Array<W>`。
* 这不仅使 `collect` 的值类型变成了 `Identifier | { into: Identifier, flatten?: Boolean }` 的异构联合体；
* 还被迫在编译期附加“仅当源集合 `source` 为 `array` 且单次字段也为非空 `array` 时才允许 `flatten: true`（`OrderedMap` 禁止使用）”的组合校验。
将二维数组展平提取为独立的内建算子 `builtin:array_flatten` 后，`for_each` 即可恢复为 100% 纯粹的一阶类型提升算子。

---

## 二、 核心设计决策（方案 B1）

### 决策 1：`ForEachSpec` 彻底剥离输出配置，仅保留输入切片与并发控制

移除 `for_each.collect` 与 `for_each.filter`（结合 [`RFC-0006`](./0006-remove-for-each-state.md) 移除的 `for_each.state`）。修改后的 `ForEachSpec` **仅包含以下 5 个纯输入与调度属性**：

```yaml
for_each:
  source: <FieldPathExpr>     # 必填：非空 Array<T> 或 OrderedMap<string, V>
  item: <Identifier>          # 必填：注入单次迭代局部帧的元素/值变量名
  key: <Identifier>           # 可选（仅当 source 为 OrderedMap 时允许）：注入的字符串键名
  index: <Identifier>         # 可选：注入的从 0 递增的整数序号名
  concurrency: <PositiveInt>  # 可选（默认 1）：最大并发激活迭代数
```

---

### 决策 2：全面复用节点自身的 `output` / `with.output` 与 `branches.<branch>.output`

挂载 `for_each` 的节点不再使用任何特殊输出语法，而是与未挂载 `for_each` 的普通节点 **100% 共享同一套输出与分支契约语法**：

1. **`ActionNode`（`run`）与 `InlineGraphNode`（`graph`）**：
   * 在节点自身的 `output: Map<Identifier, TypeSpec>` 中直接声明目标变量名 `<var>` 及其**单次迭代产出的元素类型 $U$**；
   * 在外层图符号表 $\Sigma_{\text{outer}}$ 中，该变量 `<var>` 的静态类型对应为经一阶同构提升后的集合类型 $\text{Lift}(T_{\text{src}}, U)$：
     $$\text{Lift}(T_{\text{src}}, U) = \begin{cases} \{ \texttt{type}: \texttt{array}, \texttt{items}: U \} & \text{当 } \text{BaseType}(T_{\text{src}}) = \texttt{array} \\ \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: U \} & \text{当 } T_{\text{src}} \text{ 为 } \texttt{OrderedMap} \end{cases}$$
   * 若节点声明了 `branches`，各正常分支通过 `branches.<branch>.output: [<var_1>, ...]` 声明当次迭代走该分支时产出的字段子集；未产出某字段 `<var>` 的分支（如 `output: []`）或被 `catch` 捕获的异常迭代，在屏障汇聚时自动跳过该次迭代，仅将实际产出了 `<var>` 的迭代结果按源集合下标顺序收集至外层集合 `<var>` 中。
2. **`CallNode`（`use: workflow:<path>`）**：
   * **废除“`CallNode` 挂载 `for_each` 时禁止声明 `with.output`”的限制**；
   * 与普通 `CallNode` 完全一致，通过 `with.output: { <target_out_field>: <outer_var> }` 将被调用子工作流单次迭代产出的 `<target_out_field>`（类型 $U = \text{target.output}[\text{target\_out\_field}]$）映射并提升为外层图的目标集合变量 `<outer_var>`（类型 $\text{Lift}(T_{\text{src}}, U)$）。

---

### 决策 3：新增确定性内建算子 `builtin:array_flatten`

将原 `collect.<field>.flatten: true` 拆解为独立的正交算子 `builtin:array_flatten`：
* **功能**：将非空二维数组 `Array<Array<W>>` 按序首尾拼接展平为一维数组 `Array<W>`；
* **入参 `source`**：非空二维列表 `{ type: array, items: { type: array, items: W } }`；
* **出参 `result`**：非空一维列表 `{ type: array, items: W }`，无分支单出边。

---

### 决策 4：范式迁移对比（Before vs. After）

#### 场景 A：多路分流与提取（原 `collect` + `branches.output`）
```yaml
# ==========================================
# 重构前（需发明单次字段名并在 collect 中二次映射）
# ==========================================
analyze_feedbacks:
  for_each:
    source: batch.feedbacks
    item: fb
    concurrency: 4
    collect:
      single_ticket: urgent_tickets
      single_summary: normal_summaries
  input: [fb]
  output:
    single_ticket:
      type: object
      properties: { id: { type: string }, reason: { type: string } }
      required: [id, reason]
    single_summary:
      type: string
  branches:
    urgent: { description: "紧急故障", output: [single_ticket] }
    normal: { description: "普通反馈", output: [single_summary] }
    spam:   { description: "垃圾信息", output: [] }
  run: "..."

# ==========================================
# 重构后（直接复用 output 与 branches.output，消除 collect）
# ==========================================
analyze_feedbacks:
  for_each:
    source: batch.feedbacks
    item: fb
    concurrency: 4
  input: [fb]
  # output 直接声明外层变量名及其单次元素类型；外层图中自动对应为 Array<Ticket> 与 Array<string>
  output:
    urgent_tickets:
      type: object
      properties: { id: { type: string }, reason: { type: string } }
      required: [id, reason]
    normal_summaries:
      type: string
  branches:
    urgent: { description: "紧急故障", output: [urgent_tickets] }
    normal: { description: "普通反馈", output: [normal_summaries] }
    spam:   { description: "垃圾信息", output: [] }
  run: "..."
```

#### 场景 B：跨文件子工作流批量调用（原 `CallNode` + `for_each.collect`）
```yaml
# ==========================================
# 重构前（挂载 for_each 时被迫禁用 with.output，改写 collect）
# ==========================================
batch_audit:
  use: workflow:./single_doc_audit.yaml
  for_each:
    source: doc_list
    item: doc
    concurrency: 8
    collect:
      audit_report: all_reports
  with:
    input: { document: doc }

# ==========================================
# 重构后（与普通 CallNode 100% 统一，直接使用 with.output）
# ==========================================
batch_audit:
  use: workflow:./single_doc_audit.yaml
  for_each:
    source: doc_list
    item: doc
    concurrency: 8
  with:
    input: { document: doc }
    output: { audit_report: all_reports }
```

#### 场景 C：大对象零拷贝过滤与死信队列收集（原 `for_each.filter`）
当需要在不让 LLM 重抄大对象的前提下按分支保留原始输入元素 `item`（或将执行失败的 `item` 收集至死信队列 `failed_items`）时，通过内联 `graph` + `builtin:assign` 显式表达零拷贝赋值：

```yaml
# ==========================================
# 重构后（内联 graph + builtin:assign 零拷贝分流原元素与死信）
# ==========================================
filter_and_process_articles:
  for_each:
    source: raw_articles
    item: article
    concurrency: 8
  output:
    valid_articles:
      type: object
      properties: { id: { type: string }, content: { type: string } }
      required: [id, content]
    failed_articles:
      type: object
      properties: { id: { type: string }, content: { type: string } }
      required: [id, content]
  branches:
    kept:    { description: "保留合格文章", output: [valid_articles] }
    dropped: { description: "丢弃不合格文章", output: [] }
    errored: { description: "处理异常进入死信队列", output: [failed_articles] }
  graph:
    nodes:
      judge:
        input: [article]
        branches:
          keep: "文章质量合格"
          drop: "文章质量不合格"
        catch:
          err: ["*"]
        run: "仅判断 variables.article 是否合格并选择 keep 或 drop 分支，无需输出原文。"

      assign_valid:
        use: builtin:assign
        with:
          input: { source: article }
          output: { value: valid_articles }

      assign_failed:
        use: builtin:assign
        with:
          input: { source: article }
          output: { value: failed_articles }

    edges:
      - start -> judge
      - judge.keep -> assign_valid
      - assign_valid -> end.kept
      - judge.drop -> end.dropped
      - judge.err -> assign_failed
      - assign_failed -> end.errored
```

#### 场景 D：二维列表展平（原 `collect.<field>.flatten: true`）
```yaml
# ==========================================
# 重构后（for_each 一阶提升 + builtin:array_flatten 展平）
# ==========================================
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
  run: "提取文章标签列表写入 output.nested_tags"

flatten_all_tags:
  use: builtin:array_flatten
  with:
    input: { source: nested_tags }
    output: { result: all_tags }
```

---

## 三、 `manual/spec/` 形式化规范逐文件修改方案

本节列出在 [`RFC-0006`](./0006-remove-for-each-state.md) 基础上，实施本 RFC 对 `manual/spec/` 各章节的具体修订内容（同时兼容现行规范与 [`RFC-0007`](./0007-explicit-symbol-declarations-and-type-checking-simplification.md) 的显式符号表模型）。

---

### 1. [`01-grammar-and-ast/01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md)

1. **第二节“图内节点（`InnerNode`）的三态鉴别与字段约束”**：
   * **彻底删除**列表下方的“**`CallNode` 挂载 `for_each` 时的 `with.output` 互斥约束**”条目（允许 `CallNode` 在挂载 `for_each` 时正常声明 `with.output`）。
2. **第五.1 节“集合遍历修饰符（`ForEachSpec`）”**：
   * 将 `ForEachSpec` 的字段定义精简为以下 5 项，**彻底删除 `filter`、`collect`、`state` 及“三通道数据出口与键存在性公理”**：
     * **`source: FieldPathExpr`（必填）**：$T_{\text{src}} = \text{TypeOf}(\Sigma_{\text{outer}}, \text{source})$ 必须满足 $\text{Nullable}(T_{\text{src}}) = \texttt{false} \;\land\; \text{BaseType}(T_{\text{src}}) \in \{\texttt{array}, \texttt{object (OrderedMap)}\}$；
     * **`item: Identifier`（必填）**：注入单次迭代局部帧的元素或值变量名；
     * **`key?: Identifier`（可选）**：仅当 $T_{\text{src}}$ 为 `OrderedMap` 时允许声明；
     * **`index?: Identifier`（可选）**：注入单次迭代序号（`0, 1, ...`，类型 `integer`）的变量名；
     * **`concurrency?: PositiveInt`（可选，默认 `1`）**：必须为正整数（$\text{concurrency} \ge 1$）。
   * **挂载 `for_each` 节点的有效输出非空约束**：
     * 挂载了 `for_each` 的节点 $N$ 必须向外层图绑定至少一个目标输出变量（即 $OutVars(N) \neq \emptyset$）：
       * 当 $N$ 为 `ActionNode` 或 `InlineGraphNode` 时：$\text{dom}(N.\text{output}) \neq \emptyset$；
       * 当 $N$ 为 `CallNode`（`use: workflow:<path>`）时：经 $N.\text{with.output}$ 绑定的有效目标变量集合非空。

---

### 2. [`01-grammar-and-ast/02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md)

1. **第二节表格“各语法位置的引用层级限制”**：
   * **彻底删除**最后一行 `for_each.filter.* / collect.* / state`。
2. **第三.4 节（原“`CallNode` 挂载 `for_each` 时的输出对接契约”）**：
   * **整节删除**。`CallNode` 无论是否挂载 `for_each`，均统一遵循第三.3 节的 `with.output` 映射与单射规则（唯一区别仅为挂载 `for_each` 时，外层目标变量的类型由单次输出类型 $U$ 提升为集合类型 $\text{Lift}(T_{\text{src}}, U)$）。

---

### 3. [`02-type-system-and-symbols/03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md)

1. **定义一阶同构集合类型提升算子 $\text{Lift}(T_{\text{src}}, U)$**：
   对于非空源集合类型 $T_{\text{src}}$ 与单次迭代产出字段类型 $U$：
   $$\text{Lift}(T_{\text{src}}, U) = \begin{cases} \{ \texttt{type}: \texttt{array}, \texttt{items}: U \} & \text{若 } \text{BaseType}(T_{\text{src}}) = \texttt{array} \\ \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: U \} & \text{若 } T_{\text{src}} \text{ 为 } \texttt{OrderedMap} \end{cases}$$
2. **挂载 `for_each` 的节点 $M$ 的符号表决议与类型校验**：
   * 设 $T_{\text{src}} = \text{TypeOf}(\Sigma_G, M.\text{for\_each.source})$，记节点 $M$ 向外层绑定的**单次迭代目标类型表 $\text{SingleTargetOut}(M) : \text{Identifier} \rightharpoonup \text{TypeSpec}$** 为：
     * 若 $M$ 为 `ActionNode` 或 `InlineGraphNode`：$\text{SingleTargetOut}(M) = M.\text{output}$；
     * 若 $M$ 为 `CallNode`（`use: workflow:<path>`）：$\text{SingleTargetOut}(M) = \{ \text{outer\_var} \mapsto \text{target.output}[f] \mid (f, \text{outer\_var}) \in M.\text{with.output} \}$。
   * **外层图符号表类型关系**：
     * 对每个 $(v_{\text{out}}, U) \in \text{SingleTargetOut}(M)$，其在外层图符号表 $\Sigma_G$ 中的类型为提升后的集合类型 $\text{Lift}(T_{\text{src}}, U)$（在现行工作队列模型下并入 $\Sigma_G$ 并校验等价；在 [`RFC-0007`](./0007-explicit-symbol-declarations-and-type-checking-simplification.md) 单遍显式符号表模型下，要求 $v_{\text{out}} \in \text{dom}(\Sigma_G)$ 并在节点类型检查阶段校验 $\Sigma_G(v_{\text{out}}) \equiv \text{Lift}(T_{\text{src}}, U)$）。
   * **对挂载 `for_each` 的 `InlineGraphNode` 的子图宿主输出契约说明**：
     * 当递归校验挂载了 `for_each` 的内联子图 $M.\text{graph}$ 时，子图自身的宿主输出契约 `host.output` 为**未提升前的单次输出映射 $M.\text{output}$**（即子图内部在每次迭代到达 `end` / `end.<branch>` 时，产出的是单次元素类型 $U = M.\text{output}[v]$；待 `for_each` 屏障汇聚后，写入父图 $\Sigma_G$ 的同名变量 $v$ 的类型为提升后的 $\text{Lift}(T_{\text{src}}, U)$）。

---

### 4. [`02-type-system-and-symbols/05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md)

在第一节中新增第 10 个内建算子 **`builtin:array_flatten`**：

#### `10. builtin:array_flatten`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须满足外层与内层均为非空列表：
    $$\text{Nullable}(T_{\text{src}}) = \texttt{false} \;\land\; \text{BaseType}(T_{\text{src}}) = \texttt{array} \;\land\; \text{Nullable}(T_{\text{src}}.\text{items}) = \texttt{false} \;\land\; \text{BaseType}(T_{\text{src}}.\text{items}) = \texttt{array}$$
    否则静态检查器在编译期报错。令内层元素类型 $W = T_{\text{src}}.\text{items}.\text{items}$。
* **出参推导 / 校验**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：一维列表类型 $T_{\text{res}} = \{ \texttt{type}: \texttt{array}, \texttt{items}: W \}$（须满足 $\Sigma(v_{\text{res}}) \equiv T_{\text{res}}$）。
* **静态控制分支推导**：
  * 无分支单出边（`output_subset = {v_res}`）。

---

### 5. [`03-static-flow-analysis/02-scoping-and-collision-rules.md`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md)

1. **第二节“`for_each` 局部帧注入名无冲突公理”**：
   * **彻底删除第 4 项“单次产出与外层图变量无遮蔽”**！
     * 原因：移除 `collect` 后，节点声明的 `output` / `with.output` 键本身就是外层图的目标集合变量名，不再存在“单次局部字段名”与“外层集合变量名”两套标识符；
     * 对于 `ActionNode`（`run`），执行器仅通过只读快照读取 `M.input` 中的变量，并通过返回载荷写出 `M.output` 中的字段，即使 `M.output` 中的外层目标集合变量名已存在于 $\Sigma_{\text{outer}}$ 中，只要该变量未被同时列入 `Injected(M.for_each)`，在单次迭代中就绝无任何类型或读写冲突；
     * 对于 `InlineGraphNode`（内联 `graph`），若某个目标集合变量 $v \in \text{dom}(M.\text{output})$ 已存在于父图 $\Sigma_{\text{outer}}$ 中，由于在父图中 $\Sigma_{\text{outer}}(v) = \text{Lift}(T_{\text{src}}, U)$ 而子图内部 $\Sigma_{\text{sub}}(v) = U$，由子图 `HostIn` 与 `host.output` 同帧同类型不变式（$\text{Lift}(T_{\text{src}}, U) \not\equiv U$）自然阻止在 $M.\text{input}$ 中同时捕获同名变量 $v$。
   * 因此，第二节仅保留简洁的 3 条注入名无冲突规则：
     1. **注入项内部互异**：`item`、`key`（若有）、`index`（若有）两两互不相同；
     2. **对 `ActionNode` 或 `CallNode`**：$\text{Injected}(M.\text{for\_each}) \cap \text{dom}(\Sigma_{\text{outer}}) = \emptyset$；
     3. **对 `InlineGraphNode`**：$\text{Injected}(M.\text{for\_each}) \cap (\text{set}(M.\text{input}) \cup \text{dom}(M.\text{output})) = \emptyset$。

---

### 6. [`03-static-flow-analysis/03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md) 与 [`04-concurrent-disjoint-write-analysis.md`](../manual/spec/03-static-flow-analysis/04-concurrent-disjoint-write-analysis.md)

1. **$OutVars(N)$ 定义彻底统一**：
   * 在 [`03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md#L25-L29) 第二节第 2 项与 [`04-concurrent-disjoint-write-analysis.md`](../manual/spec/03-static-flow-analysis/04-concurrent-disjoint-write-analysis.md#L10-L13) 第一节中，**删除“若 $N$ 未挂载 `for_each`……若 $N$ 挂载了 `for_each`……”的二元区分**！
   * 无论节点 $N$ 是否挂载 `for_each`，其向所属外层图写入的顶层目标变量集合 $OutVars(N)$ 统一定义为：
     * 当 $N$ 为 `ActionNode` 或 `InlineGraphNode` 时：$OutVars(N) = \text{dom}(N.\text{output})$；
     * 当 $N$ 为 `CallNode` 时：$OutVars(N) = \text{values}(N.\text{with.output})$（非空目标变量集合）。
2. **删除 [`04-concurrent-disjoint-write-analysis.md`](../manual/spec/03-static-flow-analysis/04-concurrent-disjoint-write-analysis.md#L21-L36) 第二节“`for_each` 内部三通道目标变量单射公理”**：
   * 原因：`ActionNode` 与 `InlineGraphNode` 的 `output` 本身为字典映射，键名天然两两互异；`CallNode` 的 `with.output` 已由 [`02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md#L84-L86) 的单射约束保证不同输出字段不会映射到同一个外层目标变量。整个第二节不再需要任何额外条款，直接删除。

---

### 7. [`04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md`](../manual/spec/04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md)

将第二节“`for_each` 同构集合遍历与保序汇聚状态机”简化为统一的**单通道同构保序汇聚算法**：

设挂载了 `for_each` 的节点为 $M$，源集合求值结果为有序序列 $S = [(k_0, v_0), (k_1, v_1), \dots, (k_{n-1}, v_{n-1})]$（当源为 `Array` 时 $k_i = i$；当源为 `OrderedMap` 时按插入顺序排列，$k_i$ 为字符串键），长度为 $n = |S|$，外层目标输出变量集合为 $OutVars(M)$：

1. **空集合边界（$n = 0$）**：
   * 若 $n = 0$，不激活任何子迭代（计 `0` 步）；
   * 对每个外层目标变量 $v \in OutVars(M)$，直接初始化为空集合（当源为 `Array` 时写入 `[]`，当源为 `OrderedMap` 时写入 `{}`）；
   * 控制令牌直接沿默认出边 `M -> target` 前进。
2. **并发窗口控制与按序槽位分配（$n \ge 1$）**：
   * 运行时分配长度为 $n$ 的有序结果槽数组 $\text{Slots}[0 \dots n-1]$，并通过信号量控制同时处于激活状态的迭代数不超过 `concurrency`。
   * **单次迭代完成记录**：
     * 若第 $i$ 次迭代**正常完成**（返回正常分支 $b_i$ 或默认无分支出口）：提取该分支契约承诺产出的字段子集（若 $M$ 为 `CallNode`，则将产出的字段按 $M.\text{with.output}$ 重映射为外层目标变量名，忽略未在 `with.output` 中绑定的字段），得到有效产出映射 $\text{out}_i$ 并记录至 $\text{Slots}[i]$；
     * 若第 $i$ 次迭代触发可恢复异常并**被 `M.catch` 拦截**（进入异常分支 $c_i \in \text{dom}(M.\text{catch})$）：当次迭代的脏修改全部回滚，将空产出 $\text{out}_i = \emptyset$ 记录至 $\text{Slots}[i]$。
3. **屏障保序汇聚（Order-Preserving Aggregation）**：
   * 待全部 $n$ 个元素迭代完成后，对每个外层目标变量 $v \in OutVars(M)$，按原始下标顺序 $i = 0, 1, \dots, n-1$ 扫描 $\text{Slots}[i]$：
     * 依次提取所有满足 $v \in \text{dom}(\text{out}_i)$ 的产出值 $\text{out}_i[v]$：
       * 当源集合为 `Array` 时，按序追加至新列表 `result_list`；
       * 当源集合为 `OrderedMap` 时，按序以原键配对插入新有序映射 `result_map[k_i] = out_i[v]`；
     * 对于不满足 $v \in \text{dom}(\text{out}_i)$ 的槽位（即第 $i$ 次迭代走了未产出 $v$ 的正常业务分支，或被 `M.catch` 捕获），直接跳过。
   * 一次性将全部 $v \in OutVars(M)$ 的汇聚集合原子提交回外层图的 $\rho_{\text{outer}}$。

---

## 四、 `manual/docs/`（指南、参考与配方）同步修订清单

1. **[`guide/03-concurrency/01-for-each.md`](../manual/docs/guide/03-concurrency/01-for-each.md)**：
   * 重写全文结构：介绍 `for_each` 的 5 个输入/调度属性（`source`、`item`、`key`、`index`、`concurrency`），说明节点自身的 `output` / `with.output` 如何在外层自动提升为 `Array<U>` / `OrderedMap<string, U>`，以及如何利用 `branches.<branch>.output` 实现多路分流与过滤、利用内联 `graph` + `builtin:assign` 实现原元素零拷贝透传、利用 `builtin:array_flatten` 实现二维列表展平。
2. **[`cookbook/02-collection-and-concurrency/02-map-filter-and-dlq.md`](../manual/docs/cookbook/02-collection-and-concurrency/02-map-filter-and-dlq.md)**：
   * 将配方中的 `for_each.filter` 与 `for_each.collect`（含 `flatten: true`）改写为原生 `output` + `branches.<branch>.output`（及 `builtin:array_flatten`）。
3. **[`reference/01-node-and-graph-fields.md`](../manual/docs/reference/01-node-and-graph-fields.md)**：
   * 移除 `CallNode` 挂载 `for_each` 时禁用 `with.output` 的说明；
   * 在 `for_each` 字段表中删除 `filter`、`collect`、`state` 三行，仅保留 `source`、`item`、`key`、`index`、`concurrency`。
4. **[`reference/builtins/`](../manual/docs/reference/builtins/index.md)**：
   * 新增 [`reference/builtins/array-flatten.md`](../manual/docs/reference/builtins/array-flatten.md) 参考页，并在 [`reference/builtins/index.md`](../manual/docs/reference/builtins/index.md) 中登记 `builtin:array_flatten`。
