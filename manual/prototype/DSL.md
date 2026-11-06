# 工作流 DSL 规范

本文定义结构化 workflow 的声明与执行语义。定义采用 YAML/JSON，拓扑图采用**单令牌结构化并发控制流图（Structured CFG）**。运行时负责静态契约与符号表校验、分支敏感定值分析、图游标调度、栈帧状态管理、异常容错与运行时防爆栈/防死环保护；执行器负责完成原子动作并返回结果。

---

## 一、 核心架构模型（概念映射）

1. **万物皆节点（Unified Node Model）**：取消顶层 `workflow` 包装壳。一个独立的工作流文件本身就是一个**根节点（Root Node）**。
2. **双层栈帧模型（Function Frame vs. Block Frame）**：
   * **函数调用栈帧（Function Frame）**：根工作流执行或通过 `use` 调用外部工作流（含运行时内建算子 `builtin:*`）时，建立**完全隔离的函数调用栈帧**。跨文件调用不共享上下文，严格通过 `input` 和 `output` 交换数据（值传递）。
   * **块级作用域栈帧（Block Frame，唯一 SESE 组合容器）**：图内嵌套的内联 `graph` 节点作为显式捕获的单入口单出口（Single-Entry-Single-Exit, SESE）块级作用域运行。激活时仅从外层图当前 `variables` 中提取其 `input: [<var>, ...]` 显式声明捕获的变量（及 `for_each` 注入项与 `state` 累积项）只读快照，结合子图自身的 `graph.variables` 初始化独立变量表；内部状态演进严格隔离在子栈帧内，仅在到达 `end`（或 `end.<branch>`）时按所选分支关联的 `output` 向外层图提交结果（若到达 `fail` 或发生未捕获异常则自动回滚丢弃子栈帧状态）。任何需要在并发分支内部或 `for_each` 单次迭代内部组合多步串联、条件分支、`while` 回边或嵌套并发的结构，统一由内联 `graph` 承载。
3. **显式输入捕获 + 分支关联输出提交（Explicit Input Capture & Branch-Scoped Output Commit）**：
   * 在 `graph` 内部，维护一张图级共享变量表 **`variables`**。
   * 图内 `run` 节点与内联 `graph` 节点通过轻量级变量名列表 **`input: [<var>, ...]`** 显式声明需要从所属图读取的顶层变量（无需重复声明 `TypeSpec` 或 `with` 映射）；运行时仅向节点注入 `input` 列表中声明的变量只读快照（及 `for_each` 注入项）。
   * 在只读数据绑定位置（`use.with.input`、`for_each.source` 及 `run` 指令文本），支持对非空强类型结构体（`TypedDict`）使用**静态字段路径投影（`<root_var>.<field_1>.<field_2>`）**，在编译期直接推导字段子类型并零开销提取字段值。
   * 图内子节点通过显式声明 **`output`**（支持通过 `branches.<choice>.output` 声明各分支产出的字段子集）提交新增或更新的顶层变量。节点正常完成后，运行时按所选分支契约校验其 `output` 并**原子合并（覆盖）**回图的 `variables` 中。

---

## 二、 强类型系统规范（严格 JSON Schema 子集）

DSL 采用**严格闭合的静态强类型系统**。所有顶层 `input`、所有 `output` 及显式声明的 `graph.variables` 均采用 **标准 JSON Schema 的严格强类型子集（`TypeSpec`）**，无需任何语法转换即可直接用于标准 `jsonschema` 校验与大模型结构化输出（Structured Outputs）。

### 1. 四种核心类型构造器与显式可空/枚举修饰

彻底禁止无结构约束的裸 `object`、无 `items` 的裸 `array`、单独的 `"null"` 类型或 `any` 类型。任何合法的 `TypeSpec` 必须属于以下四种闭合构造之一，并支持原生 JSON Schema 的 **`enum`（枚举约束）** 与 **`type: [<BaseType>, "null"]`（显式可空类型 `Optional<T>`）**：

| 类型分类 | 标准 JSON Schema 构造形式 | 语义与强类型约束 |
| :--- | :--- | :--- |
| **1. 基础标量**<br>(Primitives & Enum) | `type: string \| integer \| number \| boolean`<br>`enum: [<literal>, ...]` (可选) | 分别对应 UTF-8 字符串、64 位有符号整数、双精度浮点数、布尔值。可选配非空数组 `enum`，要求所有枚举值字面量必须与所声明的基础标量类型严格匹配。 |
| **2. 强类型列表**<br>(Homogeneous Array) | `type: array`<br>`items: <TypeSpec>` | 同质元素有序列表。**声明 `type: array` 时，`items` 为必填字段**，且其值必须是一个合法的递归 `TypeSpec`。 |
| **3. 强类型结构体**<br>(TypedDict Object) | `type: object`<br>`properties: {<field>: <TypeSpec>}`<br>`required: [<field>, ...]` (可选) | 固定字段名、各字段拥有独立强类型的记录结构（等价于 `TypedDict` / `struct`）。`properties` 必须非空；未显式声明 `required` 时默认 `properties` 中所有字段均为必填，且运行时自动视为闭合结构（禁止额外未声明字段）。 |
| **4. 强类型有序映射**<br>(Ordered Mapping) | `type: object`<br>`additionalProperties: <TypeSpec>` | 动态字符串键（`string`）、同质值的**有序字典（Ordered Map，运行时严格保持插入顺序）**。`additionalProperties` 的值必须是一个合法的递归 `TypeSpec`（严禁设为布尔值 `true`）。 |
| **5. 显式可空类型**<br>(Nullable / `Optional<T>`) | `type: [<BaseType>, "null"]`<br>*(其余配套字段与对应 `<BaseType>` 相同)* | 等价于 `T \| null`（其中 `<BaseType>` 为 `string`、`integer`、`number`、`boolean`、`array`、`object` 之一）。**可空性完全纳入静态类型检查**：`[string, "null"]` 与 `string` 属于不同类型，不可直接赋给仅接受非空 `string` 的入参，不可直接进行 `.field` 静态字段路径投影，可空集合也不可直接作为 `for_each.source` 的源集合遍历（若需在图控制流中将外部输入的 `Optional<T>` 静态收窄为非空 `T`，可使用内建算子 `builtin:unwrap` 拆包/合并默认值，详见 `builtins.md`）。 |

> **核心互斥铁律（区分 TypedDict 与 Ordered Mapping）**：凡基础类型为 `object`（含 `type: object` 或 `type: [object, "null"]`）的 `TypeSpec`，**必须且只能在 `properties`（代表固定字段的 TypedDict 结构体）与 `additionalProperties`（代表动态键的同质 Ordered Mapping）中二选一**，严禁两者同时省略或同时声明。

### 2. YAML 中的类型声明示例（100% 合法 JSON Schema）

```yaml
# 1. 基础标量与强类型枚举（enum）
retry_count:
  type: integer
  description: "重试次数"

priority:
  type: string
  enum: [low, medium, high]

# 2. 显式可空类型（Optional<T>：type: [<BaseType>, "null"]，用于表示外部可选输入或真实可空业务字段）
reference_notes:
  type: [string, "null"]

optional_profile:
  type: [object, "null"]
  properties:
    id: { type: integer }
    name: { type: string }
  required: [id, name]

# 3. 强类型列表（Array of Strings / Nested Array）
tags:
  type: array
  items:
    type: string

score_matrix:
  type: array
  items:
    type: array
    items:
      type: number

# 4. 强类型结构体（TypedDict：type: object + properties）
review_result:
  type: object
  properties:
    passed: { type: boolean }
    score: { type: number }
    comments:
      type: array
      items: { type: string }
  required: [passed, score, comments]

# 5. 元素为 TypedDict 的数组（type: array + items 为 TypedDict，内含 enum）
analyzed_items:
  type: array
  items:
    type: object
    properties:
      index: { type: integer }
      sentiment:
        type: string
        enum: [positive, neutral, negative]
    required: [index, sentiment]

# 6. 强类型有序映射（Ordered Mapping：type: object + additionalProperties）
chapter_word_counts:
  type: object
  additionalProperties:
    type: integer

user_profiles:
  type: object
  additionalProperties:
    type: object
    properties:
      age: { type: integer }
      roles:
        type: array
        items: { type: string }
    required: [age, roles]
```

### 3. 静态类型等价性判定（Type Equivalence）与静态字段路径投影（Static Field Projection）

#### (1) 静态类型等价性（Type Equivalence）
两个 `TypeSpec` 静态等价，当且仅当满足以下全部条件：
1. **基础类型与可空性一致**：两者的基础类型完全相同（变量间的 `integer` 与 `number` 不隐式等价），且是否包含 `"null"`（可空性）完全一致（注意：`type: [string, "null"]` 与 `type: ["null", string]` 等价，但与 `type: string` 不等价）。
2. **标量枚举约束一致**：若为标量类型，两者要么均未声明 `enum`，要么声明的 `enum` 唯一值集合完全相同。
3. **复合结构递归等价**：
   * **强类型列表（`array`）**：两者的 `items` 子 Schema 递归等价。
   * **强类型有序映射（`object` + `additionalProperties`）**：两者的 `additionalProperties` 子 Schema 递归等价。
   * **强类型结构体（`object` + `properties`）**：采用结构化等价（Structural Equivalence），当且仅当两者的 `properties` 字段名集合完全相同、生效的必填字段集合（`required`）完全相同，且每个同名字段的子 Schema 递归等价。

#### (2) 静态字段路径投影（Static Field Path Projection）
当一个变量的类型为**非空强类型结构体（`TypedDict`，即 `type: object` + `properties`）**时，支持在只读数据引用位置通过点号路径 **`<root_var>.<field_1>.<field_2>...`** 直接投影访问其内部嵌套字段：
1. **允许使用静态字段路径的位置**：
   * `for_each.source`（如 `source: current_chapter.paragraphs`，直接遍历结构体内部的列表或有序映射字段）；
   * `use` 节点的 `with.input.<param>`（含调用外部子工作流与所有 `builtin:*` 内建算子，如 `raw_text: current_chapter.body`、`source: review_result.passed`）；
   * `run` 节点的指令文本（如 `variables.current_chapter.title`）。
2. **静态路径合法性与类型推导规则**：
   * 设路径表达式为 $E = v_0 . f_1 . f_2 \dots f_k$（$k \ge 1$），其中根变量为 $\text{RootVar}(E) = v_0$。
   * 对于每一步投影 $i \in \{1, \dots, k\}$，$v_0 . f_1 \dots f_{i-1}$ 的静态类型**必须是非空强类型结构体（`type: object` + `properties`）**，且字段名 $f_i$ 必须存在于该结构体的 `properties` 及生效的 `required` 集合中；最终整个路径表达式 $E$ 的静态类型即推导为最末级字段 $f_k$ 的 `TypeSpec`。
   * **严禁不安全穿透**：禁止对**显式可空结构体**（`type: [object, "null"]`，须先经 `builtin:unwrap` 拆包为非空对象）、**强类型有序映射**（`additionalProperties` 动态键）、**列表**（`array`）或**基础标量**进行 `.field` 投影，否则在编译期直接报错。
3. **写入目标保持顶层原子性**：所有变量写入位置（`output`、`with.output`、`for_each.filter`、`for_each.collect`、`for_each.state`）以及块级捕获声明列表（`run.input`、`graph.input`）**仅接受顶层变量名 `<var_name>`**（不接受点号路径），以确保图变量表的原子覆盖提交与并发写集合互斥分析的简洁严密。

---

## 三、 节点（Node）分类、分支敏感契约与内联简写

根据所处位置与执行体类型（`run`、`graph`、`use` 三选一），节点分为 **根节点（顶层独立工作流）** 和 **图内节点（`graph.nodes` 成员）**。

### 1. 根节点（文件顶层，拥有完整函数契约）

作为独立文件入口的根节点，必须显式声明完整的对外签名（等价于函数签名 `fn(input) -> (branch, output_subset)`），其他工作流可通过 `use` + `with` 调用该节点：

| 字段 | 契约 |
| :--- | :--- |
| `input` | **必填**，输入变量声明表 `{<name>: <TypeSpec>}`，可为空 `{}`。 |
| `output` | **必填**，输出变量声明表 `{<name>: <TypeSpec>}`，可为空 `{}`。 |
| `branches` | 可选，控制分支声明 `{<choice>: <BranchSpec>}`，默认 `{}`。支持分支关联输出子集（见第 3 小节）。 |
| `timeout_ms` / `retry` | 可选，当顶层是 `run` 时支持声明单次尝试超时与原地重试策略（详见 `error_handling.md`）。 |
| `run` \| `graph` | **二选一**：直接作为单步动作执行（`run`），或展开为一张控制流图（`graph`）。 |

* **当顶层是 `run` 时（原子工作流）**：动作内读取 `input.<name>`，将结果写入所选分支承诺产出的 `output.<name>`。
* **当顶层是 `graph` 时（复合图工作流）**：进入图时，`input` 中的变量值自动注入为该图 `variables` 的初始项；图到达 `end`（或 `end.<choice>`）时，运行时自动从当前图的 `variables` 中按该出口分支承诺产出的 `output` 字段集提取同名变量作为工作流返回值。

### 2. 图内节点（`graph.nodes` 的成员）

图内节点活在所属 `graph` 的栈帧中。由于所属图的符号表已维护所有变量的 `TypeSpec` 且不存在跨文件命名空间重映射，图内 `run` 与内联 `graph` 节点**无需声明 `with` 或重复书写 `input` 的 `TypeSpec`**，而是通过**轻量级顶层变量名列表 `input: [<var_name>, ...]`** 显式声明读取/捕获的顶层变量：

| 节点类型 | 鉴别字段 | 允许声明的契约字段 | 执行与栈帧语义 |
| :--- | :--- | :--- | :--- |
| **动作节点**<br>(Action) | `run` | `input` (可选，顶层变量名列表 `[<name>, ...]`，默认 `[]`)<br>`output` (可选，`{<name>: <TypeSpec>}`，默认 `{}`)<br>`branches` (可选)<br>`for_each` (可选)<br>`timeout_ms` / `retry` / `catch` (可选) | 运行时仅向执行器注入 `input` 列表中显式声明的 `variables.<name>` 只读快照（可含外层图变量及 `for_each` 注入的 `item`/`key`/`index`）；通过 `output.<name>` 提交对图变量的更新或新增。若仅做条件分支路由、集合元素过滤（`for_each.filter`）或无返回值副作用，可省略 `output` 或设为空 `{}`。 |
| **内联子图**<br>(Subgraph) | `graph` | `input` (可选，外层顶层变量名列表 `[<name>, ...]`，默认 `[]`)<br>`output` (可选，支持**字段名列表简写 `[<name>, ...]`** 或 `{<name>: <TypeSpec>}`，默认 `{}`)<br>`branches` (可选)<br>`for_each` (可选)<br>`timeout_ms` / `retry` / `catch` (可选) | **块级子栈帧（Block Frame，唯一 SESE 组合容器）**：仅继承 `input` 列表中显式声明捕获的外层图变量只读快照 + `for_each` 注入项（若有）+ `for_each.state` 累积项（若有）+ 子图 `graph.variables` 初始化独立变量表（未声明捕获的外层变量对子图完全不可见）；子图到达 `end`（或 `end.<choice>`）时，按该分支关联的 `output` 提取变量（若含 `for_each` 则经 `filter` / `collect` / `state` 处理后）提交回外层图。**支持 `output: [var_a, var_b]` 简写，直接从子图内部符号表自动推导 `TypeSpec`，免去内外层重复声明类型**。 |
| **调用节点**<br>(Call) | `use` | `with` (可选)<br>`for_each` (可选)<br>`timeout_ms` / `retry` / `catch` (可选，仅限 `workflow:<path>`) | **函数调用栈帧（Function Frame）**：引用 `workflow:<path>` 或内建算子 `builtin:<op>`，契约与分支继承自目标定义。完全不穿透外层 `variables`，统一通过 `with.input` / `with.output`（或默认同名，支持静态字段路径投影）适配参数，并在挂载 `for_each` 时透明叠加 `filter` / `collect` / `state`。 |

* **图内 `run` 与内联 `graph` 的 `input: [<name>, ...]` 静态约束**：
  * `input` 列表必须为去重的顶层变量名字符串数组（如 `input: [topic, review_feedback]`）。
  * **对于图内 `run` 节点**：`input` 中的每个名字必须属于所属图的静态符号表，或属于该节点自身 `for_each` 注入的 `item`、`key`、`index`；且 `run` 指令文本中引用的所有 `variables.<name>`（或静态字段路径 `variables.<name>.<field>...` 的根变量 `<name>`）**必须已包含在该节点的 `input` 列表中**，否则编译期直接报错。
  * **对于内联 `graph` 节点**：`input` 列表用于声明从**外层图**捕获的顶层变量，列表中每个名字必须属于外层图的静态符号表（该节点自身 `for_each` 注入的 `item`、`key`、`index` 以及 `for_each.state` 列出的状态变量会自动注入子图初始上下文，无需重复列入内联 `graph` 节点的 `input` 中）。

### 3. 分支关联输出契约（Branch-Scoped Output / Tagged Union）

为消除早退、跳过或未命中分支被迫用 `[T, "null"]` 和 `null` 兜底初值污染正常业务分支的非空类型契约，DSL 将**分支敏感定值（Branch-Sensitive Definite Assignment）**确立为全局统一机制。在任何允许声明 `branches` 的位置（根节点、`run` 节点、内联 `graph` 节点），每个分支 `<choice>` 支持两种声明形式：

1. **全量产出简写（`<choice>: "<description>"`）**：
   * 当分支值为字符串描述时，默认该分支承诺产出节点 `output` 中声明的**全部字段**。
2. **显式输出子集契约（`<choice>: { description: "<description>", output: [<field_1>, ...] }`）**：
   * 显式声明当节点走 `<choice>` 分支时承诺产出并定值的 `output` 字段子集（列表中的每个字段名必须已在节点的 `output` 中声明；若该分支无需产出任何字段，可声明 `output: []`）。
   * **示例**：
     ```yaml
     output:
       final_article: { type: string }   # 纯正非空 string，无需因 aborted 分支降级为 [string, "null"]
       audit_report: { type: string }
       total_revisions: { type: integer }
     branches:
       published:
         description: "审查通过并发布"
         output: [final_article, audit_report, total_revisions]
       aborted:
         description: "超过最大修改次数，放弃发布"
         output: [total_revisions]       # aborted 分支仅产出修改次数，无需产出文章与报告
     ```

---

## 四、 图的符号表、分支敏感定值分析与状态演进（`graph.variables`）

`graph` 定义包含四个字段：`variables`（可选）、`max_steps`（可选，单图最大步数上限；无环内联子图天然有界，无需配置）、`nodes`（必填）、`edges`（必填）。

### 1. 两阶段静态符号表汇总（Two-Phase Symbol Table Resolution）

在执行前，运行时按从外到内的顺序扫描每张图，通过以下**两阶段规则**构建该图的**静态符号表（变量名 -> TypeSpec）**，既保证强类型严格等价，又支持字面量初值与目标类型的自然兼容：

#### 第一阶段：收集显式与推导 `TypeSpec` 并执行同帧严格等价校验
扫描当前图的以下四类类型来源：
1. **宿主上下文输入**：
   * 若为根节点的 `graph`：来自顶层 `input` 声明 `{<name>: <TypeSpec>}`。
   * 若为内联子图的 `graph`：**仅继承该内联 `graph` 节点在 `input: [<name>, ...]` 中显式声明捕获的外层变量及其 `TypeSpec`**（绝不隐式继承未列入 `input` 的其他外层变量），以及该节点 `for_each`（若有）注入的 `item`、`key`、`index` 与 `for_each.state` 累积变量。
2. **`graph.variables` 中显式带类型的声明项（`<name>: { <TypeSpec>, default: <literal> }`）**：当初始值为空数组 `[]`、空映射 `{}`、`null`（且未在其他 `output` 声明类型），或该变量仅被读取而未在任何 `output` 中声明类型时，通过此形式显式声明完整的 JSON Schema `TypeSpec` 与 `default`（例如 `history: { type: array, items: { type: string }, default: [] }`、`cache: { type: object, additionalProperties: { type: integer }, default: {} }`）。
3. **图内普通节点的 `output` 与宿主 `output`**：
   * 当前图的宿主 `output` 契约，以及未挂载 `for_each` 的 `run`、内联 `graph` 或 `use`（经 `with.output` 映射后，含 `builtin:*` 泛型推导出的目标变量）写回图中的变量。
   * **内联 `graph` 的 `output` 列表简写推导**：当内联 `graph` 节点的 `output` 采用字段名列表简写 `output: [var_a, var_b]` 时，静态检查器直接从该子图的内部符号表中提取 `var_a`、`var_b` 的 `TypeSpec` 作为该内联 `graph` 节点的对外输出类型（若采用完整映射 `{ var_a: <TypeSpec> }`，则校验其与子图内部 `var_a` 的 `TypeSpec` 严格等价）。
4. **图内 `for_each` 节点的 `filter`、`collect` 与 `state` 目标变量（详见第六节）**：
   * **`for_each.filter`（原元素零拷贝过滤/分流目标）**：每个目标变量 `<target_var>` 的类型直接继承自 `for_each.source` 的集合类型（源为 `{ type: array, items: T }` 时目标亦为 `{ type: array, items: T }`；源为 `{ type: object, additionalProperties: TV }` 时目标亦为 `{ type: object, additionalProperties: TV }`）。
   * **`for_each.collect`（单次产出收集提升目标）**：挂载了 `for_each` 的节点，其单次局部产出字段名**仅存在于单次迭代局部帧内，绝不写入外层图符号表**；运行时将单次产出类型 `U` 按源集合容器形状提升后注册到外层图符号表：
     * **默认提升（`flatten: false`）**：单次产出类型 `U` 在源为列表时提升为 **`{ type: array, items: U }`**，在源为有序映射时提升为 **`{ type: object, additionalProperties: U }`**（仅收集选择了产出该字段分支的迭代项）；
     * **单层扁平化提升（`flatten: true`，仅限源为列表）**：要求单次产出为列表 `{ type: array, items: U }`，展开拼接后提升为一维列表 **`{ type: array, items: U }`**。
   * **`for_each.state`（串行累积折叠目标）**：与外层图已定值的同名状态变量 `TypeSpec` 严格等价，原地演进外层变量。

> **同帧同名同类型铁律**：在同一张图的局部符号表内，若某个变量名在上述第一阶段的多个来源中均出现了 `TypeSpec`，则这些 `TypeSpec` 必须满足**静态类型严格等价（Type Equivalence）**，否则在编译期报错。由此确立每个已声明变量的唯一目标 `TypeSpec`。

#### 第二阶段：`graph.variables` 字面量简写兼容性校验与标量兜底推导
对于 `graph.variables` 中使用**字面量简写（`<name>: <literal>`）**的项（如 `retry_count: 0`、`feedback: ""`）：
1. **当 `<name>` 已在第一阶段获得显式/推导 `TypeSpec` 时（实例兼容性校验）**：
   * 静态检查器**不再从字面量反向推导独立类型**，而是直接校验 `<literal>` 是否为该变量目标 `TypeSpec` 的**合法实例（Schema Instance Compatibility）**：
     * 若目标 `TypeSpec` 为显式可空类型 `type: [<BaseType>, "null"]`，则初值 `<literal>` 既允许为 `null`，也允许为符合 `<BaseType>`（及配套约束）的非空字面量；非空类型严禁将初值设为 `null`。
     * 若目标 `TypeSpec` 为 `type: number`（或 `[number, "null"]`），允许使用整数字面量（如 `0`）或浮点数字面量（如 `0.0`）作为初值。
     * 若目标 `TypeSpec` 包含 `enum` 约束，初值 `<literal>` 必须属于该 `enum` 的合法枚举值集合（或当类型可空时为 `null`）。
2. **当 `<name>` 未在第一阶段任何显式 `TypeSpec` 处出现时（纯内部标量状态兜底推导）**：
   * 此时要求 `<literal>` 必须为**非空基础标量字面量**（`string` / `integer` / `number` / `boolean`），静态检查器直接将其类型推导为对应的无 `enum` 基础标量 `TypeSpec`（若需空数组 `[]`、空映射 `{}`、可空类型或 `enum` 约束，必须使用显式带类型声明 `{ <TypeSpec>, default: <literal> }`）。

#### 宿主初值互斥与局部作用域无冲突规则
* **宿主输入与 `graph.variables` 初值互斥（消除初值覆盖歧义）**：
  * 凡属于当前图**宿主上下文输入**的变量（即根图的顶层 `input`，或内联 `graph` 节点的 `input: [<name>, ...]` 捕获变量、`for_each` 注入的 `item`/`key`/`index` 及 `for_each.state` 累积变量）在进入图时已由外层提供确定的运行期值，**严禁在当前图的 `graph.variables` 中重复声明同名变量**。
  * 若内联 `graph` 节点对外声明了 `output` 导出变量 `X`：
    * **场景 A（读取并原地演进外层已定值变量 `X`）**：将 `X` 列入内联 `graph` 节点的 `input: [..., X]`（或 `for_each.state: [..., X]`）中。此时进入子图时直接继承外层 `X` 的当前运行期值（且禁止在子图 `graph.variables` 中重复声明 `X`）。
    * **场景 B（由子图首次初始化或完全重写 `X`，不读取外层旧值）**：不在内联 `graph` 节点的 `input` 中列入 `X`。此时子图可根据自身需要在子图的 `graph.variables` 中为 `X` 声明初值，或由子图内部节点在通往对应 `end` 分支的路径上完成对 `X` 的赋值。
* **块级作用域封装与局部命名无冲突（Scoped Non-Collision）**：
  * **内联 `graph` 的私有变量完全独立于未捕获的外层变量**：由于未列入内联 `graph` 节点 `input: [...]` 的外层变量对子图完全不可见，子图的私有变量（如内层循环独立的计数器 `retry_count` 等）**允许与外层图中未捕获的变量同名**，从而保障块级作用域的模块化封装。
  * **同帧注入无冲突**：内联 `graph` 上挂载 `for_each` 时，注入的 `item`、`key`、`index` 之间不可互相同名，且**不可与该内联 `graph` 节点 `input: [...]` 显式捕获的变量、`for_each.state` 变量或对外 `output` 字段同名**；普通 `run` 或 `use` 节点挂载 `for_each` 时，其单次迭代帧注入的 `item`、`key`、`index` 不可互相同名，且不可与外层图已有的 `variables` 重名。
* **禁止隐式读写**：节点绝不能读取未在 `input`（或 `with.input` / `for_each.source` / `for_each.state`）中声明的变量，也绝不能在 `output`（及 `for_each.filter` / `for_each.collect` / `for_each.state`）之外隐式修改或创建图变量。

### 2. 分支敏感静态定值检查（Branch-Sensitive Definite Assignment Analysis）

运行时在执行前对每张图的 CFG 独立进行全路径分支敏感静态数据流定值分析（Must-Definite Assignment Analysis），在编译期 100% 拦截任何未初始化读取（确保运行时绝不出现 `undefined`）：

1. **图起点初始定值集 $Def(\text{start})$**：
   * 对于根节点的 `graph`：$Def(\text{start}) = \text{dom}(\text{top\_level.input}) \cup \text{dom}(\text{graph.variables})$。
   * 对于内联子图的 `graph`：$Def(\text{start}) = \text{set}(\text{inline\_node.input}) \cup \text{Injected}(\text{inline\_node.for\_each}) \cup \text{set}(\text{inline\_node.for\_each.state}) \cup \text{dom}(\text{subgraph.variables})$。
   * 注意：仅在子图 `output` 中声明、但既未列入内联子图 `input`/`state` 也未在子图 `graph.variables` 中赋初值的变量，在子图 `start` 处处于**未定值（Unassigned）**状态，绝不会因外层符号表存在该名字而发生“未定值穿透”。
2. **分支敏感出边定值传播（Branch-Sensitive Edge Transfer）**：
   * 当无分支节点 $N$（或挂载了 `for_each` 的节点 $N$）正常完成并沿默认出边 $e = (N \to \text{target})$ 前进时，该出边的定值集为 $Def_{out}(e) = Def_{in}(N) \cup OutVars(N)$（其中 `for_each` 节点的 $OutVars(N)$ 为其 `filter`、`collect`、`state` 的全部外层目标变量）。
   * 当未挂载 `for_each` 的有分支节点 $N$（含自定义 `run`、内联 `graph`、`use` 外部工作流或 `builtin:*`）沿正常分支出边 $e_c = (N.\text{choice} \to \text{target})$ 前进时，**仅将该分支契约 `branches.<choice>.output` 所关联的产出变量子集 $OutVars(N, \text{choice})$ 加入定值集**：
     $$Def_{out}(e_c) = Def_{in}(N) \cup OutVars(N, \text{choice})$$
   * 当未挂载 `for_each` 的节点 $N$ 声明了 `catch` 并沿异常捕获出边 $e_{err} = (N.\text{<catch\_branch>} \to \text{target})$ 前进时，该分支等价于 `output: []`（不产出任何节点输出变量），定值集恒为 $Def_{out}(e_{err}) = Def_{in}(N)$（详见 `error_handling.md`）。
3. **节点入口定值保证（Entry Definite Assignment）**：
   * 节点 $N$ 的静态读取顶层变量集 $Use(N)$ 由其声明契约唯一确定（当 `for_each.source` 或 `with.input` 使用静态字段路径 `<root_var>.<field>...` 时，取其根变量 $\text{RootVar}(\cdot)$）：
     * 图内 `run` 节点：$Use(N) = \text{set}(N.\text{input}) \cup \{\text{RootVar}(N.\text{for\_each.source})\} \cup \text{set}(N.\text{for\_each.state})$；
     * 内联 `graph` 节点（在外层图视角）：$Use(N) = \text{set}(N.\text{input}) \cup \{\text{RootVar}(N.\text{for\_each.source})\} \cup \text{set}(N.\text{for\_each.state})$；
     * `use` 调用节点：$Use(N) = \text{RootVars}(N.\text{with.input}) \cup \{\text{RootVar}(N.\text{for\_each.source})\} \cup \text{set}(N.\text{for\_each.state})$。
   * 对于图内任意节点 $N$ 及其读取集中的每个顶层变量 `<name>` $\in Use(N)$，从所属图 `start` 到达节点 $N$ 的**每一条可达控制流路径**上，`<name>` 必须已被初始化（即 `<name>` 属于节点 $N$ 所有入边定值集的交集 $Def_{in}(N) = \bigcap_{e \in in\_edges(N)} Def_{out}(e)$）。
4. **`end` / `end.<choice>` 与 `fail` 出口定值规则**：
   * 从 `start` 到达无分支 `end` 的每一条可达路径上，当前图宿主 `output` 声明的全部变量必须均已定值。
   * 从 `start` 到达分支出口 `end.<choice>` 的每一条可达路径上，**仅要求当前图宿主 `branches.<choice>` 契约所关联的 `output` 变量子集均已定值**（未关联到该早退分支的其余 `output` 变量无需在该路径上定值，也无需在 `graph.variables` 中设置 `null` 兜底初值）。
   * 从 `start` 到达异常回滚出口 `fail` 的路径触发当前图栈帧丢弃回滚，**不要求**宿主 `output` 变量已定值。

### 3. 运行时按需注入与原子提交

* **最小权限只读注入**：节点激活执行期间，运行时仅从当前图 `variables`（及 `for_each` 局部帧）中提取该节点 `input`（或 `with.input`）显式声明的变量（或静态字段投影值）只读快照注入执行上下文，未声明的变量对执行器不可见。
* **按分支契约原子验证写回**：只有当节点正常执行完毕、且其当前所选分支对应的 `output` 字段子集通过了 JSON Schema 类型校验后，运行时才将结果**原子合并（覆盖同名项）**到当前图的 `variables` 中。节点执行失败或进入 `catch` 分支不会污染图的 `variables`。

---

## 五、 子工作流调用（`use`）与内建算子（`builtin:*`）

### 1. 外部子工作流调用与 `with` 映射（`use: workflow:<path>`）

`use` 接收资源引用 `workflow:<path>`。由于外部子工作流拥有独立的 `input` 和 `output` 命名空间，调用节点通过可选的 `with` 进行静态入参绑定与出参映射（省略时默认按**同名变量匹配**，并在编译期校验入参/出参的 `TypeSpec` 等价性）：

```yaml
nodes:
  normalize_doc:
    use: workflow:normalize
    with:
      # 输入绑定：<子工作流 input 参数名>: <当前上下文变量名 | 静态字段路径 | 非字符串字面量 | { value: 任意字面量 }>
      input:
        raw_text: user_document.body      # 支持绑定顶层变量名或非空 TypedDict 的静态字段路径
        max_length: 2000                  # 直接传递非字符串常量字面量（integer/number/boolean/null）
        format_style: { value: "strict" } # 显式传递字符串或复合常量字面量
      # 输出适配：<子工作流 output 变量名>: <当前节点产出的变量名>
      output:
        clean_text: normalized_document
```

* **`with.input` 传参规则与作用域**：
  * **解析作用域**：当 `use` 未挂载 `for_each` 时，`with.input`（及默认同名匹配）从当前图的 `variables` 中查找变量；**当 `use` 挂载了 `for_each` 时，`with.input`（及默认同名匹配）从单次迭代的局部上下文（即外层图 `variables` 只读快照 + `for_each.state` 当前累积值 + 本次迭代注入的 `item`、`key` 与 `index`）中查找变量**。
  * **省略 `with.input`（或省略某入参项）**：自动从上述解析作用域中提取与目标 `input` 同名的顶层变量传入（须通过静态定值检查与强类型等价检查）。
  * **变量名与静态字段路径绑定（字符串表达式）**：当值为字符串 `var_name` 或点号路径 `root_var.field_1.field_2` 时，解析为引用上述解析作用域中的顶层变量或非空 `TypedDict` 的嵌套字段（如绑定 `for_each` 注入的结构体字段 `current_chapter.body`），其推导出的 `TypeSpec` 必须与目标入参的 `TypeSpec` 严格等价。
  * **常量字面量传递**：当值为数值（`integer` / `number`）、布尔值（`boolean`）、`null`，或使用 **`{ value: <literal> }`** 显式包装的任意字面量（含字符串字面量、数组、有序映射、结构体对象；注：`with.input.<param>` 下的任何 YAML 对象映射一律解析为仅含 `value` 键的字面量包装器）时，直接作为常量值传入子工作流。常量字面量采用与 `graph.variables` 相同的**实例兼容性校验**，无需预先在 `graph.variables` 中声明伪变量。
* **`with.output` 统一写回与 `for_each` 透明衔接规则**：
  * `with.output` 始终负责将目标工作流的顶层 `output` 字段名映射为当前节点的产出字段名（省略 `with.output` 或省略某项时，默认保持目标 `output` 原字段名不变；不同输出项不可映射到同一个名字）。
  * **当 `use` 未挂载 `for_each` 时**：映射后的产出字段（按所选分支关联的 `output` 子集）直接原子写回当前图的顶层 `variables`。
  * **当 `use` 挂载了 `for_each` 时**：`for_each` 作为透明的外围包装器，直接消费被调用工作流的 `branches`（及 `catch` 异常分支）与映射后的单次产出字段，按与 `run` / `graph` 完全相同的规则执行 `for_each.filter`（原元素零拷贝过滤）、`for_each.collect`（收集提升）或 `for_each.state`（串行累积更新）。

### 2. 运行时内建算子概览（`use: builtin:<op>`）

对于显式可空类型拆包/合并默认值、基于标量/枚举的确定性条件路由、以及 `while` 动态条件循环中的集合累积写入，运行时提供零开销、确定性的内建泛型算子，统一通过 `use: builtin:<op>` + `with` 调用，并遵循统一的**泛型类型推导**与**分支关联输出契约**（完整规范与示例见独立文档 **`builtins.md`**）：

| 内建算子 | 适用场景 | 泛型输入 / 输出推导 | 控制分支与定值语义 |
| :--- | :--- | :--- | :--- |
| **`builtin:unwrap`** | 将显式可空类型 `Optional<T>` 按是否非空分支拆包，或通过 `default` 直接合并为非空 `T` | 输入：`source` (`[<BaseType>, "null"]`), `default?` (`T`)<br>输出：`value`（去除 `"null"` 后的非空类型 `T`） | **未传 `default`（双分支）**：`.some`（`output: [value]`）下 `value` 获得非空定值；`.none`（`output: []`）下不写入 `value`<br>**传入 `default`（无分支单出边）**：若为 `null` 则取 `default`，`value` 直接获得非空定值 |
| **`builtin:switch`** | 对已有的 `boolean` 或带 `enum` 约束的 `string`（或结构体字段）做零开销排他分支路由 | 输入：`source` (`boolean` 或带 `enum` 的 `string`)<br>输出：无 (`output` 为空) | **`boolean`**：固定暴露 **`.true`** / **`.false`** 分支<br>**`enum`**：自动暴露与各枚举值同名的 **`.<enum_val>`** 分支 |
| **`builtin:array_shift`** | 在 `while` 循环中零开销从列表头部弹出首元素与剩余列表（支持短路早退遍历与动态工作队列） | 输入：`source` (`Array[T]`)<br>输出：`item` (`T`), `rest` (`Array[T]`，允许与 `source` 同名原地更新) | **双分支**：`.some`（`output: [item, rest]`）下 `item` 与 `rest` 获得定值；`.empty`（`output: []`）下不写入 |
| **`builtin:array_append`** | 在 `while` 循环或串行流程中零开销将单个元素追加到列表末尾 | 输入：`source` (`Array[T]`), `item` (`T`)<br>输出：`result` (`Array[T]`，允许与 `source` 同名原地更新) | **无分支（单出边）**：将 `item` 追加到 `source` 末尾写入 `result`，提供 `result` 定值 |
| **`builtin:map_set`** | 在 `while` 循环或串行流程中零开销向有序映射插入或更新键值对 | 输入：`source` (`OrderedMap[string, TV]`), `key` (`string`), `value` (`TV`)<br>输出：`result` (`OrderedMap[string, TV]`，允许与 `source` 同名原地更新) | **无分支（单出边）**：在 `source` 中设置 `key -> value` 写入 `result`，提供 `result` 定值 |

---

## 六、 集合遍历：原元素过滤（`filter`）、产出收集提升（`collect`）与串行状态折叠（`state`）

当需要对图中的某个**非空强类型列表（`{ type: array, items: T }`）**或**非空强类型有序映射（`{ type: object, additionalProperties: TV }`，运行时为 Ordered Map）**（支持顶层变量名或非空 `TypedDict` 的静态字段路径 `<root_var>.<field>...`）逐项执行动作（`run`）、内联子图（`graph`）或外部子工作流（`use`）时，统一在节点上声明 `for_each`（注意：可空集合如 `type: [array, "null"]` 须先经 `builtin:unwrap` 拆包后方可作为 `for_each.source`）。

`for_each` 采取统一的**单次迭代局部帧注入（`source -> item / key / index`）**，并在内部**消费单次迭代的 `branches`（及 `catch` 异常分支）**（对外层 `graph.edges` 始终呈现单一无分支出口 `node -> target`），在输出端正交提供三类数据通道：
1. **`filter`（按迭代分支零拷贝保留原元素：`Array<T> -> Array<T>`，支持多路分流 `Partition` 与异常死信队列隔离）**
2. **`collect`（收集节点内产出的新数据并提升为集合：`Array<T> -> Array<U>`，配合迭代分支关联输出天然支持条件收集与 `flatten` 展开拼接）**
3. **`state`（跨元素串行累积折叠：`Fold / Reduce`，强制 `concurrency: 1`）**

```yaml
nodes:
  # 形态 A：纯过滤与多路分流（Filter / Partition：Array<T> -> Array<T>，零拷贝保留原 item，无需声明 output）
  triage_articles:
    for_each:
      source: feed_payload.articles # 类型为 { type: array, items: ArticleStruct }
      item: current_article         # 拆包注入当前元素（ArticleStruct）
      concurrency: 5
      # 按单次迭代所选的 branch，由运行时零拷贝将原 current_article 收入对应目标列表（走 drop 分支的自动丢弃）
      filter:
        keep: valid_articles        # 类型自动推导为与 source 相同的 Array<ArticleStruct>
        flagged: review_queue       # 类型自动推导为与 source 相同的 Array<ArticleStruct>
    input: [current_article]
    branches:
      keep: "符合发布规范，保留"
      flagged: "疑似违规，转入人工复核队列"
      drop: "垃圾内容，直接丢弃"
    run: "审阅文章《variables.current_article.title》与正文 variables.current_article.body，选择 keep、flagged 或 drop 分支。"

  # 形态 B：转换收集与条件收集（Map / 条件 Collect / FlatMap：映射在 run 内进行，按分支决定产出哪些字段）
  analyze_articles:
    for_each:
      source: valid_articles
      item: current_article
      index: idx
      concurrency: 5
      collect:
        # 1. 全量保形收集（所有分支均产出 score）：score (number) -> all_scores (Array<number>)
        score: all_scores
        # 2. 条件收集（仅 high_quality 分支产出 featured_summary）：自动只收集走 high_quality 分支产出的纯正非空 string
        featured_summary: featured_summaries
        # 3. 单层扁平化收集（flatten: true）：article_tags (Array<string>) -> all_tags (Array<string>)
        article_tags:
          into: all_tags
          flatten: true
    input: [idx, current_article]
    output:
      score:
        type: number
      featured_summary:
        type: string
      article_tags:
        type: array
        items: { type: string }
    branches:
      high_quality:
        description: "得分 >= 80，额外产出精选摘要"
        output: [score, featured_summary, article_tags]
      standard:
        description: "得分 < 80，无需产出精选摘要"
        output: [score, article_tags]
    run:
      - "评估第 variables.idx 篇文章 variables.current_article.title，打分写入 output.score，提取标签列表写入 output.article_tags。"
      - "若得分 >= 80，提炼精选摘要写入 output.featured_summary，选择 high_quality 分支；否则直接选择 standard 分支。"

  # 形态 C：有状态串行累积折叠（Sequential Fold：通过 state 跨元素演进外层变量，强制 concurrency: 1）
  read_chapters_sequentially:
    for_each:
      source: book_data.chapters
      item: current_chapter
      index: chapter_idx
      concurrency: 1                # 声明 state 时，concurrency 必须为 1（默认值）
      state: [rolling_summary]      # 声明跨元素原地累积更新的外层已定值变量列表
      collect:
        single_note: chapter_notes  # 可同时配合 collect 收集每章产出的阅读札记
    input: [chapter_idx, current_chapter, rolling_summary]
    output:
      rolling_summary:
        type: string
      single_note:
        type: string
    run:
      - "阅读第 variables.chapter_idx 章内容：variables.current_chapter。"
      - "结合截至上一章的累积摘要 variables.rolling_summary，融入本章新增剧情写入 output.rolling_summary，并撰写本章札记写入 output.single_note。"

  # 形态 D：遍历强类型有序映射（OrderedMap[string, TV] -> OrderedMap[string, U]，同样支持 filter / collect / state）
  translate_sections:
    for_each:
      source: section_map      # 必填，源变量名或静态字段路径（类型为 { type: object, additionalProperties: { type: string } }）
      key: section_id          # 遍历有序映射时可选，拆包注入当前键（类型为 string）
      item: section_body       # 必填，拆包注入当前值（类型为 additionalProperties 声明的 TV）
      index: idx               # 可选，注入有序遍历序号 0, 1, ...（类型为 integer）
      concurrency: 4
      collect:
        translated_body: translated_section_map
    input: [section_id, section_body]
    output:
      translated_body:
        type: string
    run: "翻译章节 variables.section_id 的正文 variables.section_body，写入 output.translated_body。"
```

**`for_each` 的统一契约与三类通道（`filter` / `collect` / `state`）规则**：
1. **局部隔离帧与入参拆包（Unpack）**：
   * 每次迭代建立独立的局部上下文：
     * 当 `source` 的静态类型为**强类型列表（`type: array`）**时：注入只读的 `variables.<item>`（类型为 `items` 定义的 `T`）和可选的 `variables.<index>`（类型 `integer`）；此时禁止声明 `key`。
     * 当 `source` 的静态类型为**强类型有序映射（`type: object` + `additionalProperties`）**时：按 Ordered Map 插入顺序遍历，注入只读的 `variables.<item>`（当前值，类型为 `additionalProperties` 定义的 `TV`）、可选的 `variables.<key>`（当前键，类型 `string`）以及可选的 `variables.<index>`（有序序号 `0, 1, ...`，类型 `integer`）。
   * **注入项无冲突原则**：`item`、`key`、`index` 之间不可互相同名；当挂载在普通 `run` 或 `use` 节点上时，不可与外层图已有的 `variables` 重名；当挂载在内联 `graph` 节点上时，不可与该内联 `graph` 节点 `input: [...]` 捕获的变量、`for_each.state` 变量或单次 `output` 字段重名（不同 `for_each` 节点的 `item` / `key` / `index` 仅活在各自迭代帧内，不写入外层 `variables`，因此兄弟 `for_each` 节点之间可以自由复用相同名字）。
   * **节点内部对注入项与外层变量的访问**：
     * 若节点为 `run`：在节点的 `input: [...]` 列表中声明需要读取的 `<item>`、`<key>`、`<index>`、`state` 变量或外层顶层变量名。
     * 若节点为内联 `graph`：`<item>`、`<key>`、`<index>` 及 `for_each.state` 变量自动作为子图的宿主输入注入子图初始 `variables`（子图内部节点通过各自的 `input: [...]` 读取它们）；若子图还需读取外层图的其他只读变量，则在外层内联 `graph` 节点的 `input: [...]` 中显式声明捕获。
     * 若节点为 `use`：单次迭代注入的 `<item>`、`<key>`、`<index>` 及 `for_each.state` 变量（及其内部结构体字段路径）可直接通过 `with.input`（或同名匹配）作为入参传给被调用的子工作流。
2. **迭代级 `branches` 与 `catch` 的内部消费及外层单出口边界**：
   * 被 `for_each` 修饰的节点（`run`、内联 `graph`、`use`）**允许声明 `branches` 与 `catch`**。
   * 这些 `branches` 与 `catch` 异常分支仅在**单次迭代粒度**生效，由 `for_each` 屏障在内部消费（用于触发 `for_each.filter` 原元素保留/死信分流，以及根据分支的 `output` 子集决定本次迭代产出哪些字段；其中 `catch` 分支等价于 `output: []`）。
   * 在所属外层图的 `graph.edges` 中，挂载了 `for_each` 的节点始终是**单一无分支出口**（只能连接 `node -> target`，严禁在外层写 `node.<choice> -> target`）。
3. **三类正交输出通道（`filter` / `collect` / `state`，可单独或组合使用）**：
   * 三类通道写回外层图的顶层目标变量名之间严禁重复（单射约束）。
   * **通道一：`filter`（按迭代分支零拷贝保留原元素，`Array<T> -> Array<T>` / `OrderedMap<K, TV> -> OrderedMap<K, TV>`）**：
     * 语法：`filter: { <branch_choice>: <target_var>, ... }`。
     * **静态校验**：每个键 `<branch_choice>` 必须是该节点 `branches` 或 `catch` 中已声明的有效分支名；每个值 `<target_var>` 为外层顶层变量标识符，静态类型自动推导为与 `for_each.source` 完全等价的集合类型。
     * **运行时语义**：按源集合顺序遍历，当第 $i$ 次迭代走入 `<branch_choice>` 分支时，运行时直接将该次迭代的原始 `item`（若源为有序映射则保留原键值对 `key: item`）**零拷贝**追加到 `<target_var>` 中（未出现在 `filter` 映射表中的其余分支不保留原元素）。
   * **通道二：`collect`（收集节点单次产出并提升为集合，`Array<T> -> Array<U>` / `OrderedMap<K, TV> -> OrderedMap<K, U>`）**：
     * 凡未列入 `state` 的单次产出字段 `<single_field>`，为仅存在于单次迭代局部帧中的局部字段（不可与外层图已有变量同名，**绝不直接写入或污染外层图的 `variables` 表**），必须通过 `for_each.collect` 收集并提升为外层集合变量 `<target_var>`。
     * 语法：`collect: { <single_field>: <target_var> | { into: <target_var>, flatten?: boolean } }`。`<target_var>` 必须为外层顶层变量标识符，不可与 `<single_field>` 同名。
     * **策略 1：默认提升（`flatten: false`）**：
       * 按源集合顺序收集**所有实际产出了 `<single_field>` 的迭代项**（若节点无 `branches`/`catch` 则为全部迭代；若有则为所选正常分支的 `output` 子集包含 `<single_field>` 的迭代，未包含 `<single_field>` 的正常分支或进入 `catch` 的异常分支自动跳过）：
       * 当 `source` 为列表时，提升为 **`{ type: array, items: U }`** 写入外层 `variables.<target_var>`（若无任何迭代产出则写入 `[]`）；
       * 当 `source` 为有序映射时，将产出该字段的迭代原键 `key` 与产出的 `U` 配对，提升为 **`{ type: object, additionalProperties: U }`** 写入外层 `variables.<target_var>`（若无任何迭代产出则写入 `{}`）。
     * **策略 2：单层扁平化提升（`flatten: true`，仅限源为列表）**：
       * **静态类型前置要求**：仅当 `source` 为列表（`type: array`）时允许启用 `flatten: true`，且单次产出字段 `<single_field>` 的类型必须为非空列表 **`{ type: array, items: U }`**。
       * **运行时语义**：按源列表下标顺序将产出该字段的各次迭代子列表 `Array[U]` 依次展开并首尾拼接，整体提升为一维列表 **`{ type: array, items: U }`** 一次性原子写入外层 `variables.<target_var>`。
   * **通道三：`state`（有状态串行累积折叠 `Fold`）**：
     * 语法：`state: [<state_var_1>, ...]`。
     * **静态并发与定值约束**：一旦声明 `state`，`for_each.concurrency` **必须为 `1`**（若显式声明 `concurrency > 1`，编译期直接报错）。`state` 中的每个变量 `<state_var>` 必须在进入该节点前已在当前外层图中**静态定值**，且节点单次产出中必须包含同名、同 `TypeSpec` 的字段 `<state_var>`。
     * **跨步演进语义**：第 0 次迭代读取外层 `variables.<state_var>` 的初始值；若第 $i$ 次迭代所选分支产出了 `<state_var>`，则立即用新值覆盖累积状态并传入第 $i+1$ 次迭代；若所选分支未产出 `<state_var>`（`output` 子集不含该字段，或进入 `catch` 异常分支），则保持上一轮累积值不变；全部迭代正常完成后，最终累积值原子写入外层 `variables.<state_var>`。

---

## 七、 结构化并发控制流图（Structured CFG）与运行时保护

`graph.edges` 定义控制令牌（Control Token）的转移拓扑。图允许包含环路（即支持动态条件收敛循环 `while`），无需满足 DAG 无环限制。

### 1. 连边语法（6 种标准形式）

| 边表达式 | 控制流与调度语义 |
| :--- | :--- |
| `source -> target` | 无分支节点（含挂载了 `for_each` 的节点或 `start`）正常完成后，控制令牌移交 `target`（`target` 可为普通节点、`end` 或 `fail`）。 |
| `source.choice -> target` | 分支排他路由：未挂载 `for_each` 的节点走对应正常业务 `branch`（携带该 `choice` 关联的 `output` 定值）或 `catch` 异常分支（不携带节点 `output` 定值）时，控制令牌移交 `target`。 |
| `source -> [worker_a, worker_b] -> join_target` | 结构化并发（Fork-Join）：`source`（可为普通无分支节点或直接为图起点 `start`）完成后同时激活全部并发节点，全部完成后令牌汇合移交 `join_target`（可为普通节点或直接为 `end` / `end.choice`）。 |
| `source.choice -> [worker_a, worker_b] -> join_target` | 选择对应 `branch` 后启动结构化并发组，完成后汇合移交 `join_target`（可为普通节点或 `end` / `end.choice`）。 |
| `source -> end` / `source -> end.choice` | 正常结束当前图栈帧，从 `variables` 提取宿主对应出口分支承诺产出的 `output` 原子提交，并返回对应的顶层 `branch`。 |
| `source -> fail` / `source.choice -> fail` | 异常中止当前图栈帧，**丢弃当前图对宿主 `output` 的一切未提交状态（Rollback）**，并向父调用栈帧抛出 `execution` 异常（详见 `error_handling.md`）。 |

### 2. 拓扑、SESE 组合与并发静态校验铁律

1. **单令牌确定性出边与 SESE 并发分支约束**：
   * `start`、非并发组内的无分支节点（含挂载了 `for_each` 的节点）、以及未挂载 `for_each` 的节点声明的每个正常分支 `node.<choice>` 与异常捕获分支 `node.<catch_branch>` **必须且只能有一条出边**。
   * 出现在并发组 `[worker_a, worker_b]` 内部的节点，其入边与汇聚出边已由所属的三元并发边 `-> [worker_a, worker_b] -> join_target` 完整定义，**禁止**再单独声明额外的入边或出边，且**并发组内的节点若未挂载 `for_each`，禁止声明对外 `branches` 或 `catch`**。
   * **内联 `graph` 作为唯一 SESE 组合容器**：若单个并发分支内部需要多步串联、条件分支、`catch` 异常恢复、`while` 循环或嵌套并发，统一将该分支声明为内联 `graph` 节点（配合 `output: [var_a, ...]` 字段名列表简写，无需重复声明子图内已有的 `TypeSpec`，无环时亦无需声明 `max_steps`），在子图内部闭环消化控制流并汇总至子图唯一的 `end`。
2. **Fork 快照隔离与并发写集合互斥（Snapshot Isolation & Disjoint Write Set）**：
   * **写集合互斥（消除 Write-Write Race）**：在同一并发组 `[worker_a, worker_b]` 中，各并发节点写回外层图 `variables` 的目标变量名集合（含普通节点的 `output`、`use` 节点的 `with.output` 目标变量、以及 `for_each.filter`、`for_each.collect` 与 `for_each.state` 的外层目标变量）**必须两两互斥（无交集）**，否则静态校验直接报错。
   * **Fork 快照读取与 Join 屏障提交（消除 Read-Write Race）**：并发组启动（Fork）瞬间，运行时锁定外层当前图 `variables` 的**只读快照**；并发组内各分支在读取外层状态（或内联子图初始化其 `input` 捕获的外层变量）时，使用的均是该 Fork 时刻的快照（内联子图内部节点在此基础上正常读写并演进子图自身的独立子栈帧），绝不会读到同组兄弟并发节点中途产出的变量。各并发节点的对外产出暂存于各自隔离缓冲中，**仅当全部并发节点正常完成到达 Join 屏障（含直接指向 `end` 的屏障）时，才一次性原子合并回外层主图 `variables`**。
   * **未捕获异常的 Fail-Fast 协同取消**：若并发组（或 `for_each`）中任一分支抛出未被该分支内部 `catch` 捕获的运行时异常，运行时立即取消同组其余未完成分支，丢弃该并发组的暂存输出，不污染当前图的 `variables`。
3. **可达性与静态防死环校验**：
   * 图内所有节点必须可从 `start` 到达（无孤立死代码）。
   * 图内所有节点（特别是处于环路中的节点）在拓扑上**必须至少存在一条路径能够到达 `end`（或 `end.<choice>`）**（编译期拦截无正常出口的绝对死循环）。

### 3. 异常处理、容错机制与运行时护栏（Error Handling & Runtime Guardrails）

关于运行时异常分类、节点级超时与重试（`timeout_ms` / `retry`）、异常边界捕获（`catch`）以及图级异常回滚终态（`fail`）的完整规范，请参见独立文档 **`error_handling.md`**。核心要点如下：

1. **三级错误与异常模型**：
   * **显式业务分支（`branches`）**：可预期的业务条件分流通过 `branches` 声明并关联各自的 `output` 子集，正常完成时原子提交（Commit）。
   * **可恢复运行时异常（`execution` / `timeout` / `contract` / `max_steps`）**：当节点执行失败、超时、违反 `output` Schema 契约或子图超出 `max_steps` 时，自动丢弃未提交的局部状态（Rollback）。节点可通过 `retry` 基于只读输入快照原地重试，或通过 `catch: { <branch>: [<selector>, ...] | "*" }` 将异常在节点边界物化为控制分支（等价于 `output: []`）；未被 `catch` 拦截的异常自动沿调用栈向上冒泡展开（Unwind）。
   * **不可恢复引擎致命终止（Panic）**：触及调用栈深度上限 `StackOverflowError`、根图自身 `MaxStepsExceededError` 或外部取消 `CancelledError` 时，严禁被 `retry` 或 `catch` 拦截，立即终止工作流。
2. **调用栈深度上限（`max_call_depth`，防递归/嵌套爆栈）**：
   * 运行时按调用树路径维护栈帧深度（根工作流为 `1`；每次沿调用链进入 `use` 或内联 `graph` 时，该执行路径的栈深度 `+1`，退出时 `-1`；并发分支或并发 `for_each` 的各分支按各自独立的调用路径计算深度，互不横向累加）。
   * 当任一执行路径的栈深度超过运行时阈值（默认 `64`）时，立即抛出不可捕获的 `StackOverflowError` 并终止执行，从而安全拦截无限直接递归（`A -> A`）或间接相互递归（`A -> B -> A`）。
3. **单图执行步数上限（`max_steps`，防动态无限循环）**：
   * 每个图栈帧维护独立的节点调度步数计数器：在该图内，每次激活一个普通节点计 `1` 步；启动一个并发组 `[worker_a, worker_b]` 按组内激活的节点数计步；带有 `for_each` 的节点按其实际执行的**元素迭代次数**累加步数（若该节点为内联 `graph` 或 `use`，其内部子栈帧同时受各自独立的 `max_steps` 约束）。
   * 当图栈帧的累计步数超过该图的 `max_steps`（默认 `1000`，可在包含环路的 `graph.max_steps` 中按需显式声明覆盖）仍未到达 `end` 时，立即中断该图栈帧并抛出 `max_steps` 异常（若为子图栈帧，可被外层包裹节点的 `catch` 捕获；若为最顶层根图栈帧，则直接终止工作流）。

---

## 八、 综合完整示例

下面用一个包含 **显式 `input` 捕获、结构体字段投影读取（`request.topic`）、`builtin:unwrap` 可空拆包、分支关联输出契约（`published` 与 `aborted` 各自产出对应的非空字段子集，彻底免除 `null` 兜底污染）、Switch 条件循环（回边）、外部调用（`use` 含字面量传参）以及结构化并发直接汇合到 `end.published`** 的完整示例展示规范的表现力：

```yaml
# 文件顶层即为根 Node（对外暴露严格的 input / output / branches 契约）
input:
  request:
    type: object
    properties:
      topic:
        type: string
      reference_notes:
        type: [string, "null"]
    required: [topic, reference_notes]
    description: "撰稿请求结构体（进入图后可通过 request.topic 等点分路径投影读取）"
output:
  final_article:
    type: string
    description: "发布版文章（仅在 published 分支产出，纯正非空 string）"
  audit_report:
    type: string
    description: "合规审查报告（仅在 published 分支产出，纯正非空 string）"
  total_revisions:
    type: integer
    description: "总修改次数（所有分支均产出）"
branches:
  published:
    description: "审查通过并发布"
    output: [final_article, audit_report, total_revisions]
  aborted:
    description: "超过最大修改次数，放弃发布"
    output: [total_revisions]

graph:
  max_steps: 50
  # 图级初始状态：仅需声明跨循环演进的计数器与首轮默认指导，无需为 aborted 分支塞入 final_article: null 假初值
  variables:
    total_revisions: 0
    review_feedback: "无（首次撰写）"

  nodes:
    # 0. 内建可空拆包算子：检查 request.reference_notes 是否非空，若非空则拆包为 string 类型的 notes
    check_notes:
      use: builtin:unwrap
      with:
        input:
          source: request.reference_notes
        output:
          value: notes

    # 0b. 当存在参考资料时，先提炼资料要点作为首轮撰写指导
    prepare_notes_guide:
      input: [notes]
      output:
        review_feedback:
          type: string
      run: "提炼参考资料 variables.notes 的核心要点，作为首轮撰稿要求写入 output.review_feedback。"

    # 1. 撰写/修改节点（处于循环环路中，显式捕获 request 与 review_feedback，通过 request.topic 投影读取结构体字段）
    write_draft:
      input: [request, review_feedback]
      output:
        draft:
          type: string
      run: "根据主题 variables.request.topic 和指导/审查意见 variables.review_feedback 撰写或修改文章，写入 output.draft。"

    # 2. 审查节点（使用分支关联输出契约：pass / give_up 无需重复产出 review_feedback）
    review_draft:
      input: [draft, total_revisions]
      output:
        review_feedback:
          type: string
        total_revisions:
          type: integer
      branches:
        pass:
          description: "质量合格"
          output: []
        revise:
          description: "需要打回修改"
          output: [review_feedback, total_revisions]
        give_up:
          description: "已修改满 3 次仍不合格，终止"
          output: []
      run:
        - "评估 variables.draft 的质量。"
        - "若合格，直接选择 pass 分支。"
        - "若不合格且 variables.total_revisions < 3，将 variables.total_revisions 加一写入 output.total_revisions，将修改建议写入 output.review_feedback，选择 revise 分支。"
        - "若不合格且已满 3 次，直接选择 give_up 分支。"

    # 3. 并行处理节点 A：排版润色（调用外部子 workflow，演示变量绑定与常量字面量传参）
    polish_format:
      use: workflow:markdown_formatter
      with:
        input:
          raw_content: draft
          line_width: 80
          style_preset: { value: "publishing" }
        output:
          formatted_content: final_article

    # 4. 并行处理节点 B：生成合规报告（与节点 A 并发，基于同一 Fork 快照读取，写回不同图变量）
    generate_audit:
      input: [draft, total_revisions]
      output:
        audit_report:
          type: string
      run: "对 variables.draft 进行合规性总结，共经历了 variables.total_revisions 次修改，生成报告写入 output.audit_report。"

  edges:
    - start -> check_notes
    # 若 request.reference_notes 非空，notes 已定值且类型为非空 string，提炼要点后进入撰写
    - check_notes.some -> prepare_notes_guide
    - prepare_notes_guide -> write_draft
    # 若 request.reference_notes 为 null，直接沿用 variables.review_feedback 的默认初值进入撰写
    - check_notes.none -> write_draft
    - write_draft -> review_draft
    # 循环回边（CFG 环路）：携带更新后的 review_feedback 与 total_revisions 跳回 write_draft
    - review_draft.revise -> write_draft
    # 超过次数直接以 aborted 分支结束整个图（aborted 仅要求定值 total_revisions）
    - review_draft.give_up -> end.aborted
    # 审查通过后，启动结构化并发同时做排版和生成报告，Join 屏障合并后以 published 分支结束
    - review_draft.pass -> [polish_format, generate_audit] -> end.published
```
