# 运行时内建算子规范（`builtin:*`）

本文定义工作流 DSL 运行时内建算子（`use: builtin:<op>`）的静态类型推导、控制分支与分支关联输出契约（Branch-Scoped Output）语义。

关于工作流整体架构、类型系统、静态字段路径投影与 `use` 调用机制，请参见 `DSL.md`；关于运行时异常与容错模型请参见 `error_handling.md`；关于循环与遍历的组合模式示例，请参见 `while_and_iter.md` 与 `while_plus_iter.md`。

---

## 一、 内建算子的设计定位与通用契约

在 DSL 中，对于显式可空类型拆包/合并默认值、基于已有标量/枚举变量的确定性条件路由、以及 `while` 动态条件循环中的集合增量消费与更新，如果完全依靠 `run` 节点去完成，不仅会产生不必要的执行器（LLM）调用开销，还需要在上下文中反复拷贝整个集合。

因此，运行时提供一组最小正交的**零开销确定性内建算子（`builtin:*`）**，具备以下通用契约：

1. **统一通过 `use` + `with` 调用**：语法形态与调用外部子工作流完全一致，严格通过 `with.input`（支持绑定顶层变量、非空 `TypedDict` 的**静态字段路径 `<root_var>.<field>...`** 或常量字面量）和 `with.output`（绑定顶层目标变量）交换数据，不穿透外层命名空间。
2. **泛型静态类型推导（Generic Type Inference）**：无需手写 `output` 的 JSON Schema `TypeSpec`，静态检查器直接根据 `with.input` 绑定的源表达式类型，在编译期自动推导出目标输出变量的精确类型并注册到当前图的静态符号表。
3. **遵循统一的分支关联输出契约（Branch-Scoped Output）**：
   * 带排他分支的拆包/拆解算子（未传 `default` 的 `builtin:unwrap` 以及 `builtin:array_shift`）遵循与自定义节点 `branches.<choice>.output` 完全相同的分支敏感定值规则：
   * 当控制流沿有效值分支（`builtin:unwrap` 的 `.some` 关联 `output: [value]`，或 `builtin:array_shift` 的 `.some` 关联 `output: [item, rest]`）前进时，拆包/拆解产出的目标变量在下游路径上被标记为**已定值（Definitely Assigned）且拥有纯正的非空类型**；
   * 当控制流沿空分支（`.none` 或 `.empty`，关联 `output: []`）前进时，运行时**不写入**目标变量，静态定值分析器也在该分支上**不提供新增目标变量的定值**，从而在编译期直接拦截空分支下游误读未定值变量的行为。
4. **通用静态约束**：
   * 所有 `builtin:*` 节点**禁止挂载 `for_each`、`timeout_ms`、`retry` 或 `catch`**（集合级全量遍历、过滤与有状态串行折叠统一使用节点的 `for_each` 修饰符，详见 `DSL.md` 第六节；内建算子为常数时间确定性内存操作，不产生运行时异常，详见 `error_handling.md`）。
   * 带有排他控制分支的内建算子（`builtin:switch`、未声明 `default` 的 `builtin:unwrap` 以及 `builtin:array_shift`）**禁止出现在并发组 `[worker_a, worker_b]` 内部**；无分支的确定性算子（声明了 `default` 的 `builtin:unwrap`、`builtin:array_append`、`builtin:map_set`）拥有唯一默认出边，可正常用于任何无分支节点位置。

| 内建算子 | 核心职责 | 等价的主流语言原语 | 控制流形态与分支契约 |
| :--- | :--- | :--- | :--- |
| **`builtin:unwrap`** | 将显式可空类型 `Optional<T>` 按是否非空分支收窄为 `T`，或通过 `default` 直接合并为非空 `T` | Rust `if let Some(v) = opt` / TS `opt ?? default` | **未传 `default`（双分支）**：`some` (`output: [value]`) / `none` (`output: []`)<br>**传入 `default`（无分支单出边）** |
| **`builtin:switch`** | 对已有的 `boolean` 或带 `enum` 约束的 `string` 做零开销确定性排他分支路由 | `if (flag)` / `switch (enum_val)` / Rust `match` | **`boolean`**：`true` / `false`<br>**`enum`**：各枚举字面量同名分支 |
| **`builtin:array_shift`** | 在 `while` 循环中零开销从列表头部弹出首元素与剩余列表（支持短路早退遍历与动态工作队列） | `[item, ...rest] = list` / `queue.popleft()` | **双分支**：`some` (`output: [item, rest]`) / `empty` (`output: []`) |
| **`builtin:array_append`** | 在 `while` 循环或串行流程中零开销将单个元素追加到列表末尾 | `list.push(item)` / `[...list, item]` | **无分支（单出边）** |
| **`builtin:map_set`** | 在 `while` 循环或串行流程中零开销向有序映射插入或更新键值对 | `map[key] = value` | **无分支（单出边）** |

---

## 二、 `builtin:unwrap`：泛型可空拆包与默认值合并算子

DSL 的静态类型系统严格区分显式可空类型 `Optional<T>`（`type: [<BaseType>, "null"]`）与非空类型 `T`。`builtin:unwrap` 支持两种工作模式：
* **模式 A（省略 `default`：条件分支拆包 `if let Some(v) = opt`）**：在控制流图层面根据变量是否为 `null` 进行排他分支路由（`.some` / `.none`），并在 `.some` 分支下将 `Optional<T>` 静态收窄为非空类型 `T`。
* **模式 B（传入 `default`：无分支默认值合并 `opt ?? default`）**：当 `source` 为 `null` 时自动使用非空兜底值 `default` 替代，作为**无分支单出边节点**直接产出非空类型 `T`，无需额外编写分支边或调用 LLM 赋默认值。

### 1. 调用契约与 YAML 示例

```yaml
nodes:
  # 模式 A（未传 default）：双分支条件拆包（.some / .none）
  unwrap_optional_articles:
    use: builtin:unwrap
    with:
      input:
        source: maybe_articles # 必填，类型为显式可空类型 [<BaseType>, "null"] 的变量或静态字段路径
      output:
        value: valid_articles  # 必填，拆包写回当前图的非空目标变量（类型自动推导为去除 "null" 后的非空类型 T）

  # 模式 B（传入 default）：无分支默认值合并（Nullish Coalescing：maybe_author ?? "佚名"）
  resolve_author_name:
    use: builtin:unwrap
    with:
      input:
        source: doc_meta.maybe_author # 必填，类型为 [string, "null"] 的变量或静态字段路径
        default: { value: "佚名" }     # 可选，绑定非空 T 类型变量/字段路径，或兼容非空 T 的常量字面量
      output:
        value: author_name            # 必填，直接获得非空 string 类型定值（单出边，无 .some/.none 分支）

  process_each_article:
    for_each:
      source: valid_articles   # 在 unwrap_optional_articles.some 分支下，valid_articles 已收窄为非空 array，可直接遍历
      item: article
      collect:
        summary: article_summaries
    input: [article, author_name]
    output:
      summary: { type: string }
    run: "概括作者 variables.author_name 的文章 variables.article，写入 output.summary。"

edges:
  - start -> resolve_author_name
  - resolve_author_name -> unwrap_optional_articles
  - unwrap_optional_articles.some -> process_each_article
  - process_each_article -> end.processed
  - unwrap_optional_articles.none -> end.skipped
```

### 2. 泛型非空类型推导与静态校验规则

1. **输入参数校验（`with.input`）**：
   * `source`（必填）：必须绑定当前图中已定值、且 `TypeSpec` 为**显式可空类型 `type: [<BaseType>, "null"]`** 的变量或静态字段路径。若绑定的表达式本身已经是非空类型，静态检查器将在编译期报错（禁止冗余拆包）。记将 `source` 的 `TypeSpec` 移除 `"null"` 后（保留全部 `items`、`properties`、`required`、`additionalProperties` 或 `enum` 约束）的纯正非空类型为 `T`。
   * `default`（可选）：若声明，可绑定已定值的非空变量/静态字段路径（其 `TypeSpec` 必须与非空类型 `T` 严格等价），或传递符合非空类型 `T` 实例兼容性校验的常量字面量（严禁传入 `null`）。
2. **输出参数与泛型非空类型推导（`with.output`）**：
   * `value`（必填）：静态检查器自动推导 `value` 目标变量的类型为**纯正非空类型 `T`**：
     * 例如：`source` 为 `{ type: [array, "null"], items: { type: string } }` $\rightarrow$ `value` 推导为 `{ type: array, items: { type: string } }`；
     * 例如：`source` 为 `{ type: [object, "null"], properties: { id: { type: integer } }, required: [id] }` $\rightarrow$ `value` 推导为 `{ type: object, properties: { id: { type: integer } }, required: [id] }`。
   * **命名与初值约束**：`value` 绑定的外层目标变量名**不可与 `source` 的根变量同名**（因 `source` 可空而 `value` 非空，遵循“同帧同名同类型”铁律）；当**未声明 `default`（模式 A）**时，**严禁在 `graph.variables` 中为 `value` 声明初值**，以确保 `.none` 分支不会因初值残留而误判为已定值。

### 3. 控制流与定值行为（模式 A vs. 模式 B）

* **模式 A：未声明 `default`（双分支 `.some` vs. `.none`）**：
  * **`some` 分支（运行时 `source != null`，关联 `output: [value]`）**：将 `source` 的非空值写入 `value` 变量，控制令牌沿 `.some` 出边前进；在下游路径上 `value` 被标记为**已定值（Definitely Assigned）且拥有纯正非空类型 `T`**。
  * **`none` 分支（运行时 `source == null`，关联 `output: []`）**：**不写入 `value` 变量**，控制令牌沿 `.none` 出边前进；`.none` 出边**不提供 `value` 的定值**，下游若在未另行赋值的情况下读取 `value` 将在编译期报错。
* **模式 B：声明了 `default`（无分支单出边 `node -> target`）**：
  * **运行时行为**：若 `source != null` 则将 `source` 的值写入 `value`，否则将 `default` 的值写入 `value`，控制令牌直接沿唯一默认出边前进。
  * **静态定值行为**：节点完成后无条件提供 `value`（非空类型 `T`）的定值，且该节点不再具有 `.some` / `.none` 分支。

---

## 三、 `builtin:switch`：确定性标量/枚举条件路由算子

当图中已有 `boolean` 标志位或带 `enum` 约束的 `string` 变量（或结构体中的对应字段，如 `review_result.passed`、`ticket.priority`）时，使用 `use: builtin:switch` 进行**零 Token 开销的确定性排他分支路由**，无需调用 LLM `run` 节点复读判断。

### 1. 调用契约与 YAML 示例

```yaml
nodes:
  # 形态 A：对布尔值（或结构体布尔字段）进行二路分支路由（.true / .false）
  check_review_passed:
    use: builtin:switch
    with:
      input:
        source: review_result.passed  # 必填，类型为非空 boolean 的变量或静态字段路径

  # 形态 B：对强类型枚举（type: string + enum）进行穷尽多路分支路由
  route_by_priority:
    use: builtin:switch
    with:
      input:
        source: ticket.priority       # 必填，类型为 { type: string, enum: [low, medium, high] }

edges:
  - start -> check_review_passed
  # boolean 自动暴露 .true 与 .false 分支
  - check_review_passed.true -> route_by_priority
  - check_review_passed.false -> handle_rejection
  # enum 自动暴露与每个枚举值字面量同名的分支（编译期执行穷尽性检查）
  - route_by_priority.high -> urgent_handler
  - route_by_priority.medium -> standard_handler
  - route_by_priority.low -> batch_queue
```

### 2. 静态校验与穷尽分支推导规则

1. **输入与输出约束**：
   * `with.input.source`（必填）：必须绑定已定值的**非空 `type: boolean`**，或带有 **`enum: [<v1>, <v2>, ...] ` 约束的非空 `type: string`**（支持顶层变量名或非空 `TypedDict` 的静态字段路径）。不接受可空类型（可先经 `builtin:unwrap` 拆包）或无 `enum` 约束的自由字符串。
   * `with.output`：**禁止声明 `with.output`**（`builtin:switch` 仅做纯控制流路由，不产生或修改任何图变量）。
2. **静态穷尽分支推导（Exhaustiveness Checking）**：
   * 当 `source` 的静态类型为 `type: boolean` 时：该节点静态暴露两个固定控制分支 **`true`** 与 **`false`**（在 `edges` 中通过 `<node>.true -> ...` 与 `<node>.false -> ...` 连边）。
   * 当 `source` 的静态类型为 `type: string, enum: [<v1>, <v2>, ...]` 时：该节点静态暴露与 `enum` 列表中各枚举值字面量完全同名的控制分支 **`<v1>`**、**`<v2>`**...
   * 根据单令牌确定性出边铁律，**静态推导出的每一个分支都必须且只能在 `graph.edges` 中声明一条出边**（编译期强制穷尽所有分支，遗漏任一枚举值或声明不存在的分支名均直接报错）。

---

## 四、 `builtin:array_shift`、`builtin:array_append` 与 `builtin:map_set`：确定性集合拆解与累积算子

在 `while` 动态条件循环中，往往需要**从待处理队列按序取出单个元素以支持命中即停（短路早退遍历）**，或将单步产出的结果**追加到列表末尾 / 写入有序映射**。使用 `builtin:array_shift`、`builtin:array_append` 与 `builtin:map_set` 可在运行时直接完成零开销的确定性拆解与更新，既保持 `for_each` 专注于无早退的全量集合遍历，又彻底避免将整个集合传入 LLM `run` 节点做全量复读拷贝。

### 1. `builtin:array_shift`：列表首元素弹出与非空分支拆解算子（用于 `while` 短路早退遍历与动态队列）

当遍历列表存在**动态早退条件**（例如：按优先级依次尝试候选节点或检索章节，一旦找到满足条件的结果即刻通过回边循环出口早退，不再处理剩余元素）或需要在循环中动态向队尾追加新任务（如广度优先搜索 BFS）时，使用 `builtin:array_shift` 配合 CFG 回边实现：

```yaml
nodes:
  # 从待尝试列表 remaining_endpoints 头部弹出一个元素 current_endpoint，并将剩余切片原地写回 remaining_endpoints
  pop_next_endpoint:
    use: builtin:array_shift
    with:
      input:
        source: remaining_endpoints   # 必填，类型为 { type: array, items: T } 的已定值变量或静态字段路径
      output:
        item: current_endpoint        # 必填，仅在 .some 分支下写入首元素（类型自动推导为元素类型 T）
        rest: remaining_endpoints     # 必填，仅在 .some 分支下写入除去首元素后的剩余列表（类型推导为 { type: array, items: T }，允许与 source 同名原地覆盖）

  try_request:
    input: [current_endpoint, payload]
    output:
      response_data: { type: string }
    branches:
      ok:
        description: "当前节点请求成功，立即早退返回结果"
        output: [response_data]
      failed:
        description: "当前节点失败，继续尝试队列中的下一个节点"
        output: []
    run: "向节点 variables.current_endpoint 发送请求 variables.payload；若成功则写入 output.response_data 并选 ok，否则选 failed。"

edges:
  - start -> pop_next_endpoint
  # 队列非空（.some）：current_endpoint 与更新后的 remaining_endpoints 均已定值，进入尝试
  - pop_next_endpoint.some -> try_request
  # 命中早退条件：直接跳出循环至 end.success，不再消费剩余的 remaining_endpoints
  - try_request.ok -> end.success
  # 当前元素未命中：沿 CFG 回边跳回 pop_next_endpoint 继续消费下一个元素
  - try_request.failed -> pop_next_endpoint
  # 队列已耗尽仍未成功（.empty）：进入失败出口
  - pop_next_endpoint.empty -> end.all_failed
```

* **静态类型校验与分支关联定值规则**：
  * `with.input.source`（必填）：必须绑定已定值的**非空强类型列表** `{ type: array, items: T }`（支持顶层变量名或非空 `TypedDict` 的静态字段路径）。
  * `with.output.item`（必填）：目标变量静态类型自动推导为列表元素类型 `T`。`item` 绑定的顶层变量名不可与 `rest` 同名，且**严禁在 `graph.variables` 中为 `item` 声明初值**（确保 `.empty` 分支不会因初值残留而误判 `item` 已定值）。
  * `with.output.rest`（必填）：目标变量静态类型自动推导为 `{ type: array, items: T }`。**允许且推荐将 `rest` 与 `source` 绑定同一个顶层列表变量名**，以在 `while` 回边循环中原地推进队列切片。
  * **双分支控制流与定值行为（`.some` vs. `.empty`）**：
    * **`some` 分支（运行时 `len(source) > 0`，关联 `output: [item, rest]`）**：将 `source[0]` 写入 `item`，将切片 `source[1:]` 写入 `rest`，控制令牌沿 `.some` 出边前进；在下游路径上 `item`（类型 `T`）与 `rest`（类型 `Array<T>`）均被标记为**已定值（Definitely Assigned）**。
    * **`empty` 分支（运行时 `len(source) == 0`，关联 `output: []`）**：**不写入 `item` 与 `rest`**，控制令牌沿 `.empty` 出边前进；`.empty` 出边**不提供 `item` 的定值**，编译期直接拦截在 `.empty` 下游误读 `item` 的行为。

### 2. `builtin:array_append`：列表末尾追加算子

```yaml
nodes:
  # 将本轮生成的 attempt_record 追加到历史记录列表 attempt_history 末尾（原地更新）
  record_attempt:
    use: builtin:array_append
    with:
      input:
        source: attempt_history   # 必填，类型为 { type: array, items: T } 的已定值变量或静态字段路径
        item: current_attempt     # 必填，类型为 T 的变量、静态字段路径或常量字面量
      output:
        result: attempt_history   # 必填，类型自动推导为 { type: array, items: T }（允许与 source 同名原地覆盖）
```

* **静态类型校验与定值规则**：
  * `with.input.source`（必填）：必须绑定已定值的**非空强类型列表** `{ type: array, items: T }`。
  * `with.input.item`（必填）：绑定的变量/静态字段路径的 `TypeSpec` 必须与列表元素类型 `T` **严格等价**（若传入常量字面量则须通过 `T` 的实例兼容性校验）。
  * `with.output.result`（必填）：静态类型自动推导为 `{ type: array, items: T }`。**允许且推荐将 `result` 与 `source` 绑定同一个顶层列表变量名**（此时该列表变量通常已在 `graph.variables` 中声明初值 `default: []`）。
  * **控制流与运行时语义**：无分支单出边节点。运行时基于当前 `source` 列表追加 `item` 生成新列表并原子写回 `variables.<result>`，在下游路径上提供 `result` 的定值。

### 3. `builtin:map_set`：有序映射键值写入算子

```yaml
nodes:
  # 将当前任务 ID 与处理结果写入有序映射 result_map（若键不存在则按序追加，若已存在则更新值）
  save_task_result:
    use: builtin:map_set
    with:
      input:
        source: result_map              # 必填，类型为 { type: object, additionalProperties: TV }
        key: current_task.id            # 必填，类型为 non-null string 的变量、静态字段路径或 { value: "..." } 字面量
        value: current_output           # 必填，类型为 TV 的变量、静态字段路径或常量字面量
      output:
        result: result_map              # 必填，类型自动推导为 { type: object, additionalProperties: TV }
```

* **静态类型校验与定值规则**：
  * `with.input.source`（必填）：必须绑定已定值的**非空强类型有序映射** `{ type: object, additionalProperties: TV }`。
  * `with.input.key`（必填）：必须为非空 `type: string`（支持变量、静态字段路径或 `{ value: "<str>" }` 字面量）。
  * `with.input.value`（必填）：绑定的变量/静态字段路径的 `TypeSpec` 必须与映射值类型 `TV` **严格等价**（若传入常量字面量则须通过 `TV` 的实例兼容性校验）。
  * `with.output.result`（必填）：静态类型自动推导为 `{ type: object, additionalProperties: TV }`。**允许且推荐将 `result` 与 `source` 绑定同一个顶层映射变量名**（此时该映射变量通常已在 `graph.variables` 中声明初值 `default: {}`）。
  * **控制流与运行时语义**：无分支单出边节点。运行时保持 Ordered Map 插入顺序：若 `key` 不存在则追加至末尾，若 `key` 已存在则原地替换对应值并保持原键位置不变，原子写回 `variables.<result>` 并提供定值。
