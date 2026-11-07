# RFC-0006: 移除 `for_each.state`，解耦无状态同构并行遍历与有状态串行循环

* **状态**：Proposed
* **所属分类**：核心原语正交化与规范简化（Language Orthogonality & Specification Simplification）
* **核心解决的问题**：移除 `for_each.state` 通道，消除 `for_each` 在“无状态数据并行（Data Parallelism）”与“有状态串行折叠（Sequential Fold）”之间的职责耦合，将跨元素有状态串行迭代完全收敛至原生 CFG 成环回边与 `builtin:array_shift` 算子，大幅削减编译期（阶段 01~03）与运行期（阶段 04）的横切特判规则。
* **关联规范模块（`manual/spec/`）**：
  * [`01-grammar-and-ast/01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md)
  * [`01-grammar-and-ast/02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md)
  * [`02-type-system-and-symbols/03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md)
  * [`03-static-flow-analysis/02-scoping-and-collision-rules.md`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md)
  * [`03-static-flow-analysis/03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)
  * [`03-static-flow-analysis/04-concurrent-disjoint-write-analysis.md`](../manual/spec/03-static-flow-analysis/04-concurrent-disjoint-write-analysis.md)
  * [`04-runtime-execution-model/01-frame-memory-model.md`](../manual/spec/04-runtime-execution-model/01-frame-memory-model.md)
  * [`04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md`](../manual/spec/04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md)
  * [`04-runtime-execution-model/04-exception-handling-and-unwinding.md`](../manual/spec/04-runtime-execution-model/04-exception-handling-and-unwinding.md)
* **关联文档模块（`manual/docs/`）**：
  * [`guide/03-concurrency/01-for-each.md`](../manual/docs/guide/03-concurrency/01-for-each.md)
  * [`guide/02-control-flow/02-cycles-and-back-edges.md`](../manual/docs/guide/02-control-flow/02-cycles-and-back-edges.md)
  * [`guide/04-state-and-resilience/01-frames-and-scoping.md`](../manual/docs/guide/04-state-and-resilience/01-frames-and-scoping.md)
  * [`guide/01-foundations/02-use-subworkflows.md`](../manual/docs/guide/01-foundations/02-use-subworkflows.md)
  * [`guide/index.md`](../manual/docs/guide/index.md)
  * [`cookbook/02-collection-and-concurrency/01-stateful-sequential-fold.md`](../manual/docs/cookbook/02-collection-and-concurrency/01-stateful-sequential-fold.md)
  * [`cookbook/index.md`](../manual/docs/cookbook/index.md)
  * [`reference/01-node-and-graph-fields.md`](../manual/docs/reference/01-node-and-graph-fields.md)

---

## 一、 问题背景与架构耦合分析

### 痛点 6.1：`for_each` 的同构数据并行模型与 `state` 的串行状态演进根本冲突
在 Axcutor 的运行时架构中，`for_each` 被定位为**同构结构化并发原语（Homogeneous Structured Concurrency）**。其核心语义是：锁定外层图栈帧的只读快照 $\rho_{\text{parent\_snap}}$，为集合中的每个元素派生相互隔离的 [`IterationFrame`](../manual/spec/04-runtime-execution-model/01-frame-memory-model.md)，并在全部迭代完成后按源集合下标顺序保序汇聚。

然而，`for_each.state` 在该原语内部塞入了**跨迭代串行数据依赖**（第 $i$ 次迭代读取第 $i-1$ 次迭代写出的 $\sigma_i$）：
1. **并发能力退化**：一旦声明非空 `state`，`concurrency` 失去调节意义，必须在编译期强制锁死为 `concurrency: 1`。
2. **破坏快照隔离不变式**：对于普通外层变量，所有迭代读取的是进入节点前锁定的静态快照 $\rho_{\text{parent\_snap}}$；唯独对于 `state` 变量，第 $i$ 次迭代读取的是动态演进的 $\sigma_i$。

---

### 痛点 6.2：`for_each.state` 在编译器与运行时五阶段中制造了 6 处横切特判
为了支撑 `for_each.state`（见 [`RFC-0003`](./0003-dataflow-and-for-each-state-semantics.md)），现行规范被迫在语法、类型系统、作用域、必达定值分析与运行时状态机中引入了大量非正交的补丁条款：

1. **阶段 01（语法与并发互斥）**：
   * 必须校验 `state` 非空时 `concurrency == 1`；
   * 必须校验 `collect` 的字段名不与 `state` 重叠（$f \notin \text{set}(N.\text{for\_each.state})$）。
2. **阶段 02（符号表工作队列双向依赖）**：
   * 普通无状态 `for_each` 节点只需等待 `source` 的根变量在 $\Sigma_G$ 中定型即可推导输出集合类型；
   * 引入 `state` 后，工作队列就绪条件被迫增加 $\text{set}(M.\text{for\_each.state}) \subseteq \text{dom}(\Sigma_G)$，并需校验 $\Sigma_G(s) \equiv \text{SingleOut}(M)[s]$。
3. **阶段 03（`ActionNode` 与 `InlineGraphNode` 的 `input` 捕获规则分裂）**：
   * 在 [`02-scoping-and-collision-rules.md`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md) 中，挂载 `for_each` 时：
     * `ActionNode`（`run`）**必须**在 `node.input` 列表中显式列出 `state` 变量；
     * `InlineGraphNode`（内联 `graph`）却**严禁**在 `node.input` 列表中列出 `state` 变量（因为 `state` 被硬编码自动并入了 $\text{HostIn}(G)$）。
   * 这种“同为外层已定值变量，在 `run.input` 中必填、在 `graph.input` 中禁填”的不对称性，极大增加了用户的心智负担与编译器的实现分支。
4. **阶段 03（单次输出字段防遮蔽规则的特例豁免）**：
   * 规范规定挂载 `for_each` 的节点单次迭代产出字段禁止与外层图符号表 $\Sigma_{\text{outer}}$ 中的变量同名，但被迫为“未列入 `for_each.state` 的局部字段”开出豁免后门（因为 `state` 要求单次输出字段名**必须**与外层图变量同名）。
5. **阶段 03（多分支下强迫所有正常分支重抄未修改的 `state` 变量）**：
   * 根据 [`03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md) 第四.3 节的“`for_each.state` 编译期分支完备性断言”，当节点声明了 `branches`（如 `updated` 与 `skipped`）时，即使 `skipped` 分支在业务逻辑上根本不需要修改 `rolling_summary`，也必须在 `skipped` 分支的 `output` 中包含 `rolling_summary`，并在 `run` 提示词中指示执行器将旧 `variables.rolling_summary` 原样抄回 `output.rolling_summary`。
6. **阶段 04（运行期 `catch` 异常捕获下的状态回退特判）**：
   * 运行时调度器除了维护按序槽位 $\text{Slots}[0 \dots n-1]$ 外，还需额外维护串行状态寄存器 $\sigma_i$，并在单次迭代进入 `catch` 分支时特判 $\sigma_{i+1} = \sigma_i$。

---

### 痛点 6.3：与原生 CFG 回边循环（`builtin:array_shift`）能力重叠且表达力受限
在 [`RFC-0004`](./0004-deterministic-data-and-collection-builtins.md) 引入 `builtin:assign`、`builtin:object_merge` 等确定性算子后，Axcutor 的 **CFG 成环回边 + `builtin:array_shift` + `builtin:array_append`** 已经构成了图灵完备、零额外心智负担的串行队列消费与状态折叠机制：

| 特性维度 | `for_each.state`（现行规范） | `CFG 回边 + builtin:array_shift` |
| :--- | :--- | :--- |
| **分支按需更新状态** | **不支持**。所有正常分支均被迫在 `output` 中包含全部 `state` 变量，未修改分支在 `run` 中需手动原样写回 | **天然支持**。不修改状态的分支直接声明 `output: []` 连回 `array_shift` 节点，外层栈帧中的状态变量天然保持原值 |
| **条件早退（`break` / 短路）** | **不支持**（见 [`RFC-0005` 痛点 5.2](./0005-control-flow-predicates-and-error-context.md)）。必须跑满全部 $n$ 个元素 | **天然支持**。循环体内任意分支均可直接连向 `end` 或下游节点跳出循环，已更新的状态与已追加的列表结果自然保留在图栈帧中 |
| **多步串联与局部回退** | 若单步折叠包含多节点，必须嵌套一层 `InlineGraphNode` | 直接在当前图平面连边，控制流一目了然 |
| **编译器/运行时专用规则** | 6 处跨阶段特判条款 | **0 处特判**（完全复用标准图边定值传递方程与 `KillVars`） |

---

## 二、 核心设计决策（方案 A1）

### 1. 彻底移除 `for_each.state`，确立正交原语边界
从 `ForEachSpec` 中移除 `state` 字段，使 Axcutor 的集合与循环原语回归严格正交的职责划分：

1. **`for_each`（纯无状态同构并行遍历）**：
   * 专职处理**无跨元素依赖的同构数据并行转换与分流**（`Map` / `Filter` / `FlatMap`）。
   * 每次元素迭代严格基于进入节点前锁定的外层只读快照 $\rho_{\text{parent\_snap}}$ 独立执行，`concurrency` 永远可自由配置为任意正整数（$\ge 1$），不再存在任何强制 `concurrency == 1` 的耦合约束。
   * 输出端仅保留两类正交集合汇聚通道：
     * **`filter`**：按单次迭代所选分支（`branches` 或 `catch`）零拷贝保留原集合元素（`Array<T> -> Array<T>` 或 `OrderedMap<string, V> -> OrderedMap<string, V>`）；
     * **`collect`**：收集单次迭代产出的局部字段并提升为外层集合（`Array<T> -> Array<U>`、`OrderedMap<string, V> -> OrderedMap<string, U>`，或 `flatten: true` 下的 `Array<Array<W>> -> Array<W>`）。
2. **`CFG 成环回边 + builtin:array_shift`（有状态串行折叠与短路循环）**：
   * 凡涉及**第 $i$ 步依赖第 $i-1$ 步产出状态**的串行累积折叠（Sequential Fold / Scan），或需在遍历途中**满足条件即提前退出（Short-Circuit / Break）**的场景，统一使用 `builtin:assign`（初始化队列）+ `builtin:array_shift`（逐项弹出）+ `builtin:array_append`（按需收集单步结果）+ CFG 回边表达。

---

### 2. 范式迁移对比示例（Before vs. After）

以“逐分片串行阅读文档、滚动更新 `rolling_summary`（跳过无关分片时不更新摘要）并收集各有效分片的札记 `section_notes`”为例：

#### 重构前（使用 `for_each.state`）：
```yaml
read_sections:
  for_each:
    source: document.sections
    item: current_section
    concurrency: 1                # 被迫锁死为 1
    state: [rolling_summary]
    collect:
      single_note: section_notes
  input: [current_section, rolling_summary]
  output:
    rolling_summary: { type: string }
    single_note: { type: string }
  branches:
    updated:
      description: "有效分片，更新全局摘要并产出札记"
      output: [rolling_summary, single_note]
    skipped:
      description: "无关分片，跳过但被迫在 output 中包含 rolling_summary"
      output: [rolling_summary]
  run: "若有效则更新 variables.rolling_summary 并写 output.single_note 选 updated；若跳过则必须将原 variables.rolling_summary 原样写入 output.rolling_summary 选 skipped。"
```

#### 重构后（使用 `builtin:array_shift` + CFG 回边）：
```yaml
graph:
  variables:
    rolling_summary: "（初始状态：无背景设定）"
    section_notes:
      type: array
      items: { type: string }
      default: []

  nodes:
    # 1. 将输入结构体中的分片列表初始化至工作队列 pending_sections
    init_queue:
      use: builtin:assign
      with:
        input:
          source: document.sections
        output:
          value: pending_sections

    # 2. 弹出首分片 current_section，剩余切片写回 pending_sections
    next_section:
      use: builtin:array_shift
      with:
        input:
          source: pending_sections
        output:
          item: current_section
          rest: pending_sections

    # 3. 处理当前分片：skipped 分支直接声明 output: []，无需重抄 rolling_summary！
    read_single_section:
      input: [document, current_section, rolling_summary]
      output:
        rolling_summary: { type: string }
        single_note: { type: string }
      branches:
        updated:
          description: "有效分片，原地更新 rolling_summary 并产出 single_note"
          output: [rolling_summary, single_note]
        skipped:
          description: "无关分片，不修改任何变量，直接进入下一轮"
          output: []
      run: "阅读分片 variables.current_section；若有效则结合 variables.rolling_summary 更新写入 output.rolling_summary 并写 output.single_note 选 updated；若无关则直接选 skipped。"

    # 4. 仅在 updated 分支下将 single_note 追加至 section_notes
    record_note:
      use: builtin:array_append
      with:
        input:
          source: section_notes
          item: single_note
        output:
          result: section_notes

  edges:
    - start -> init_queue
    - init_queue -> next_section
    - next_section.some -> read_single_section
    - read_single_section.updated -> record_note
    - record_note -> next_section                # 更新并记录后回边继续下一分片
    - read_single_section.skipped -> next_section # 跳过时直接回边，rolling_summary 保持原值不变
    - next_section.empty -> end                  # 队列消费完毕，退出循环
```

---

## 三、 `manual/spec/` 形式化规范修改方案

本节列出 `manual/spec/` 下受影响的全部 9 个规范文件的具体修订内容。

---

### 1. [`01-grammar-and-ast/01-document-and-node-grammar.md`](../manual/spec/01-grammar-and-ast/01-document-and-node-grammar.md)

1. **第二节“图内节点的三态鉴别与字段约束”**：
   * 将“`CallNode` 挂载 `for_each` 时的 `with.output` 互斥约束”中的说明由：
     > （单次迭代输出直接由 `for_each.collect` 与 `for_each.state` 对接被调用工作流的 `output` 字段）
     改为：
     > （单次迭代输出直接由 `for_each.collect` 对接被调用工作流的 `output` 字段）
2. **第五.1 节“集合遍历修饰符（`ForEachSpec`）”**：
   * **`concurrency?: PositiveInt`**：移除“当声明了非空 `state` 时，必须满足 $\text{concurrency} = 1$”，简化为：
     > 必须为正整数（$\text{concurrency} \ge 1$）。
   * **两通道数据出口与键存在性公理**（原“三通道数据出口与键存在性公理”）：
     * **数据出口非空约束**：将 `filter`、`collect`、`state` 三者不可同时为空简化为 `filter` 与 `collect` 二者不可同时为空：
       $$\text{dom}(\text{filter}) \cup \text{dom}(\text{collect}) \neq \emptyset$$
     * **`collect` 约束**：移除“且不得与 `state` 变量重叠（$\land\; f \notin \text{set}(N.\text{for\_each.state})$）”，简化为：
       $$\forall f \in \text{dom}(\text{collect}), \quad f \in \text{dom}(\text{SingleOut}(N))$$
     * **彻底删除 `state?: List<Identifier>` 及其子条款**（包括“多分支下的 `state` 完备性约束”）。

---

### 2. [`01-grammar-and-ast/02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md)

1. **第二节表格**：
   * 将最后一行 `for_each.filter.* / collect.* / state` 改为 `for_each.filter.* / collect.*`。
2. **第三.4 节“`CallNode` 挂载 `for_each` 时的输出对接契约”**：
   * 标题第 2 点改为“**由 `for_each.collect` 直接对接 `target.output`**”；
   * 删除关于 `for_each.state` 的第 3 条子项，将“未在 `for_each.collect` 或 `for_each.state` 中引用的其余 `target.output` 字段在迭代结束后自动丢弃”改为“未在 `for_each.collect` 中引用的其余 `target.output` 字段在迭代结束后自动丢弃”。

---

### 3. [`02-type-system-and-symbols/03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md)

1. **第一.2 节阶段 2 第 3 项“挂载了 `for_each` 的子节点 $M$”**：
   * **就绪条件**：移除 $\land\; \text{set}(M.\text{for\_each.state}) \subseteq \text{dom}(\Sigma_G)$，简化为：
     $$\text{RootVar}(M.\text{for\_each.source}) \in \text{dom}(\Sigma_G)$$
     （当 $M$ 为 `InlineGraphNode` 时，同时满足 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_G)$）。
   * **推导与推进**：
     * 第 2 步中，当 $M$ 为 `InlineGraphNode` 时，子图初始宿主符号表移除 $M.\text{for\_each.state}$：
       $$\Sigma_{\text{host}}(M) = \Sigma_{\text{outer}}|_{M.\text{input}} \uplus \Sigma_{\text{injected}}(M)$$
     * 第 3 步中，“三通道外层目标变量”改为“两通道（`filter` 与 `collect`）外层目标变量”，删除 `state` 目标变量的等价校验条款。
2. **第二.2 节“三类节点的内部解析符号表与 `input` 捕获校验规则”**：
   * 第 1 项对 `ActionNode`（`run`）的说明中移除 `、`state`` 字样；
   * 第 2 项对 `InlineGraphNode`（内联 `graph`）的说明简化为：
     * 子图初始宿主输入符号表为：
       $$\Sigma_{\text{host}}(M) = \Sigma_{\text{outer}}|_{M.\text{input}} \uplus \Sigma_{\text{injected}}(M)$$
     * 要求 $\text{set}(M.\text{input}) \subseteq \text{dom}(\Sigma_{\text{outer}})$，且由于 $\Sigma_{\text{injected}}(M)$ 已自动注入子图初始上下文，禁止在 $M.\text{input}$ 中重复列出注入变量：
       $$\text{set}(M.\text{input}) \cap \text{dom}(\Sigma_{\text{injected}}(M)) = \emptyset$$

---

### 4. [`03-static-flow-analysis/02-scoping-and-collision-rules.md`](../manual/spec/03-static-flow-analysis/02-scoping-and-collision-rules.md)

1. **第一节“宿主上下文输入与 `graph.variables` 初值互斥公理”**：
   * 当 $G$ 为内联 `graph` 节点 $N$ 的图时，移除 $\cup\; \text{set}(N.\text{for\_each.state})$：
     $$\text{HostIn}(G) = \text{set}(N.\text{input}) \cup \text{Injected}(N.\text{for\_each})$$
2. **第二节“`for_each` 局部帧注入名无冲突公理”**：
   * 第 3 项（当 $M$ 为 `InlineGraphNode` 时）：移除 $\cup\; \text{set}(M.\text{for\_each.state})$：
     $$\text{Injected}(M.\text{for\_each}) \cap (\text{set}(M.\text{input}) \cup \text{dom}(M.\text{output})) = \emptyset$$
   * 第 4 项（原“单次非 `state` 产出与外层图变量无遮蔽”）：移除“非 `state`”特例限定，直接表述为统一的**“单次迭代产出与外层图变量无遮蔽”**：
     > 挂载了 `for_each` 的节点 $M$，其单次迭代产出的局部字段名 `<single_field>`（待由 `collect` 提升），禁止与外层图符号表 $\Sigma_{\text{outer}}$ 中的变量同名（包含不可与 `collect` 映射的目标集合变量 `<target_var>` 同名）。
3. **第三节表格“挂载 `for_each` 时三类节点的 `input` 捕获与内部引用校验准则”**：
   * 移除 `ActionNode` 与 `InlineGraphNode` 行中所有关于 `state` / `for_each.state` 的文字与集合并集项。

---

### 5. [`03-static-flow-analysis/03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)

1. **第二节第 2 项**：
   * 将“若 $N$ 挂载了 `for_each`：$OutVars(N)$ 为 $N.\text{for\_each}$ 中 `filter`、`collect` 与 `state` 声明的全部外层目标变量集合”改为“`filter` 与 `collect` 声明的全部外层目标变量集合”。
2. **第四节标题与内容**：
   * 章节标题改为：`## 四、 节点读取与图出口的编译期定值断言`；
   * **第 1 小节“节点入口读取集断言（Entry Use Assertion）”**：从 `ActionNode`、`InlineGraphNode`、`CallNode` 的 $Use(N)$ 公式中全部移除 $\cup\; \text{set}(N.\text{for\_each.state})$：
     * `ActionNode`（挂载 `for_each`）：$Use(N) = (\text{set}(N.\text{input}) \setminus \text{Injected}(N.\text{for\_each})) \cup \{\text{RootVar}(N.\text{for\_each.source})\}$；
     * `InlineGraphNode`：$Use(N) = \text{set}(N.\text{input}) \cup \{\text{RootVar}(N.\text{for\_each.source}) \text{ (若有)}\}$；
     * `CallNode`（挂载 `for_each`）：$Use(N) = (R_{\text{with}} \setminus \text{Injected}(N.\text{for\_each})) \cup \{\text{RootVar}(N.\text{for\_each.source})\}$。
   * **彻底删除第 3 小节“`for_each.state` 编译期分支完备性断言（State Branch Completeness Assertion）”**。

---

### 6. [`03-static-flow-analysis/04-concurrent-disjoint-write-analysis.md`](../manual/spec/03-static-flow-analysis/04-concurrent-disjoint-write-analysis.md)

1. **第一节**：
   * 当 $W_i$ 挂载了 `for_each` 时，$OutVars(W_i)$ 改为其 `for_each.filter` 与 `for_each.collect` 的全部外层目标变量名集合。
2. **第二节（原“`for_each` 内部三通道目标变量单射公理”）**：
   * 标题改为：`## 二、 for_each 内部两通道目标变量单射公理`；
   * 移除 $V_{\text{state}}$ 及第 2 条“`state` 并发度排他约束”，仅保留 `filter` 与 `collect` 内部单射及通道间无交集约束：
     $$V_{\text{filter}} \cap V_{\text{collect}} = \emptyset$$

---

### 7. 运行时模型（`04-runtime-execution-model/` 下 3 个文件）

1. **[`01-frame-memory-model.md`](../manual/spec/04-runtime-execution-model/01-frame-memory-model.md)**：
   * **第一.2 节 `BlockFrame`**：移除 `for_each.state` 当前值 $\rho_{\text{state}}$，初始内存绑定简化为：
     $$\rho_0 = \rho_{\text{parent\_snap}}|_{N.\text{input}} \uplus \rho_{\text{iter}} \uplus \rho_{\text{subgraph\_defaults}}$$
   * **第一.3 节 `IterationFrame`**：移除“与当前 `state` 值”及“非 `state` 字段”表述，所有单次迭代统一基于外层只读快照 $\rho_{\text{parent\_snap}}$ 叠加 `{ item, key?, index? }`。
2. **[`02-fork-join-and-for-each-scheduler.md`](../manual/spec/04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md)**：
   * **第二节标题**改为：`## 二、 for_each 同构集合遍历与两通道保序汇聚状态机`；
   * 移除初始状态映射 $\sigma_0$ 的定义；
   * **第 1 步（空集合边界 $n = 0$）**：移除关于 `state` 目标变量保持 $\sigma_0$ 的描述；
   * **第 2 步（并发窗口控制与按序槽位分配 $n \ge 1$）**：移除所有关于 $\sigma_i \to \sigma_{i+1}$ 状态传递方程与 `M.catch` 下 $\sigma_{i+1} = \sigma_i$ 的特判；每次迭代完成后仅需将 $(\text{choice}_i, \text{out}_i)$ 写入 $\text{Slots}[i]$；
   * **第 3 步（屏障保序汇聚）**：移除“`state` 提交”子项，仅汇聚 `filter` 与 `collect` 并原子提交回 $\rho_{\text{outer}}$。
3. **[`04-exception-handling-and-unwinding.md`](../manual/spec/04-runtime-execution-model/04-exception-handling-and-unwinding.md)**：
   * **第二节 `catch` 分支匹配规则**：移除括号中“（若节点 $N$ 挂载了 `for_each` 且声明了 `state`，当次迭代被 `N.catch` 捕获时保持上一轮累积状态 $\sigma_{i+1} = \sigma_i$ 不变……）”的尾注。

---

## 四、 `manual/docs/`（指南、参考与配方）及 `prototype/` 修改方案

### 1. 用户指南（`manual/docs/guide/`）

1. **[`guide/03-concurrency/01-for-each.md`](../manual/docs/guide/03-concurrency/01-for-each.md)**：
   * 导语中“提供三类正交数据汇聚通道：**`filter`**、**`collect`** 与 **`state`**”改为“提供两类正交数据汇聚通道：**`filter`** 与 **`collect`**”；
   * 第一节中移除“未声明 `state` 时可设为 `> 1`”及 `NOTE` 提示框中关于 `state` 在 `run` 与内联 `graph` 中的差异说明；
   * 第二节标题改为 `## 二、 两类正交输出通道（filter / collect）`，**完整删除第 二.3 小节“`state`：跨元素有状态串行折叠（`Sequential Fold`）”**（含 `read_chapters_sequentially` 与 `fold_with_branch_and_catch` 两个局部示例）；在文末提示若需跨元素有状态串行折叠或条件早退，请使用 [成环回边与队列消费算子](../02-control-flow/02-cycles-and-back-edges.md)。
2. **[`guide/02-control-flow/02-cycles-and-back-edges.md`](../manual/docs/guide/02-control-flow/02-cycles-and-back-edges.md)**：
   * 在第二节“循环与流水线中的确定性数据与集合操作算子”中，明确说明 `builtin:assign` + `builtin:array_shift` + `builtin:array_append` 不仅支持短路搜索（Short-Circuit），同时也是表达**跨元素有状态串行折叠（Stateful Sequential Fold）**的标准方式。
3. **[`guide/04-state-and-resilience/01-frames-and-scoping.md`](../manual/docs/guide/04-state-and-resilience/01-frames-and-scoping.md)**：
   * 移除第 15、20、21、101 行中提及 `for_each` 自动注入 `state` 的表述，内联 `graph` 挂载 `for_each` 时自动注入的变量仅限 `item` / `key` / `index`。
4. **[`guide/01-foundations/02-use-subworkflows.md`](../manual/docs/guide/01-foundations/02-use-subworkflows.md)** 与 **[`guide/index.md`](../manual/docs/guide/index.md)**：
   * `02-use-subworkflows.md` 第 84 行移除 `或 for_each.state`；
   * `guide/index.md` 第 15 行将 `for_each（filter / collect / state）` 改为 `for_each（filter / collect）`。

---

### 2. 实战配方（`manual/docs/cookbook/`）

1. **[`cookbook/02-collection-and-concurrency/01-stateful-sequential-fold.md`](../manual/docs/cookbook/02-collection-and-concurrency/01-stateful-sequential-fold.md)** 与 **[`cookbook/index.md`](../manual/docs/cookbook/index.md)**：
   * 将配方 01 的实现方式从 `unwrap + for_each.state` 更新为 **`builtin:unwrap` + `builtin:assign` + `builtin:array_shift` + `builtin:array_append` + CFG 回边**（见本文第二节的重构后完整范式）；
   * 同步更新 [`cookbook/index.md`](../manual/docs/cookbook/index.md) 第 15 行的核心原语列描述为 `builtin:unwrap` + `builtin:assign` + `builtin:array_shift` + `builtin:array_append` + 成环回边。

---

### 3. 语法参考字典（`manual/docs/reference/`）与历史原型（`manual/prototype/`）

1. **[`reference/01-node-and-graph-fields.md`](../manual/docs/reference/01-node-and-graph-fields.md)**：
   * 第 65 行（`input` 字段表）：移除关于 `state` 的说明；
   * 第 83 行（`with.output.<out_field>`）：移除 `与 for_each.state`；
   * 第 93~96 行（`for_each` 子字段表）：
     * `concurrency`：描述简化为“最大并发度（正整数，默认 `1`）”；
     * `collect`：描述简化为“收集单次迭代产出的字段并提升为外层集合变量 `<target_var>`（`filter` 与 `collect` 二者不可同时为空）”；
     * **删除 `state` 行**。
2. **历史原型（`manual/prototype/`）**：
   * 若同步维护 `manual/prototype/`，则移除 `DSL.md`、`while_and_iter.md`、`while_plus_iter.md`、`error_handling.md` 中的 `for_each.state` 描述，将 `while_and_iter.md` 中的示例二改写为 `builtin:array_shift` 回边折叠。

---

## 五、 可选配套增强评估：`builtin:map_shift`

移除 `for_each.state` 后：
* 对于**列表（`Array<T>`）**的有状态串行遍历，已由 `builtin:array_shift` 100% 覆盖；
* 对于**有序映射（`OrderedMap<string, V>`）**，绝大多数场景是无状态并发遍历（如并发翻译各章节映射 `for_each.collect`）或在循环中按动态键随机访问更新（`builtin:map_get` / `builtin:map_set` / `builtin:map_delete`）。
* 若未来需要支持“按插入顺序逐个弹出 `OrderedMap` 键值对执行有状态串行折叠”，可在 `builtin:*` 中对称增补一个零开销算子 **`builtin:map_shift`**：
  * **入参**：`source: OrderedMap<string, T_V>`；
  * **出参**：`key: string`、`value: T_V`、`rest: OrderedMap<string, T_V>`；
  * **分支与定值**：暴露 `.some`（定值 `key`、`value`、`rest`）与 `.empty`（通过 `KillVars` 擦除 `key` 与 `value` 定值）。
  * 该算子与 `builtin:array_shift` 完全同构，无需对语法或调度器引入任何新概念。
