# 01. 图栈帧与命名空间（Stack Frames & Scoping）

在组合使用跨包子工作流（`use`）、内联子图（`graph`）与集合遍历（`for_each`）时，Axcutor 通过严格的栈帧隔离边界、显式变量表声明、宿主输入互斥规则与全路径必达定值分析，在编译期排除变量命名冲突与未初始化读取。

---

## 一、 三类栈帧与作用域隔离边界

运行时通过三类栈帧管理变量的生命周期与可见性（底层内存模型与形式化语义详见 [规范：`04-runtime-execution-model/01-frame-memory-model.md`](../../../spec/04-runtime-execution-model/01-frame-memory-model.md)）：

1. **函数调用栈帧（`Function Frame` —— 根工作流与 `use` 调用）**：
   * 每次通过 `use` 调用外部工作流（或 `builtin:*`）时，都会创建与调用方完全隔离的独立命名空间。
   * 被调用方不可直接读取父图的任何变量，仅接收通过 `with.input` 显式传入的值，并在退出时通过 `with.output` 显式写回父图。
2. **块级作用域栈帧（`Block Frame` —— 内联 `graph` 节点）**：
   * 内联 `graph` 节点采用**显式按名捕获（Explicit Capture）**：仅列入内联 `graph` 节点 `input: [<var>, ...]` 的外层变量（以及挂载 `for_each` 时自动注入的 `item` / `key` / `index`）会进入子图的初始 `variables` 表。
   * **未捕获变量隔离与同名无冲突**：未列入 `input: [...]` 的外层变量对子图不可见。子图内部可声明与外层未捕获变量同名的私有变量（如 `temp_buffer`、`retry_count`），两者位于不同栈帧，互不覆盖。
3. **单次迭代局部隔离帧（`Iteration Frame` —— 挂载 `for_each` 的 `run` 与 `use` 节点）**：
   * 当 `run` 或 `use` 节点挂载 `for_each` 时，运行时会为每个待遍历元素创建独立的单次迭代局部帧，在外层图只读变量的基础上注入当前元素的 `item` / `key` / `index` 局部变量（为避免遮蔽冲突，`item` / `key` / `index` 不可与外层图已有变量同名）。
   * 各次元素迭代之间相互隔离，单次迭代的产出在全部元素执行完毕后由屏障统一按序归集回外层图。关于 `run`、`use` 与内联 `graph` 三类节点挂载 `for_each` 时的 `input` / `output` 声明规则差异，请参阅 [01. 同构结构化并发：集合遍历（`for_each`）](../03-concurrency/01-for-each.md)。

```yaml
graph:
  variables:
    # 外层图的私有计数变量与局部中间变量
    retry_count:
      type: integer
      default: 0
    clean_text:
      type: string

  nodes:
    # 1. Function Frame：通过 with.input / with.output 跨命名空间显式传值
    external_step:
      use: workflow:common.sanitize_input
      with:
        input:
          raw: request_text
        output:
          cleaned: clean_text

    # 2. Block Frame：仅捕获 clean_text，未捕获外层的 retry_count
    inner_stage:
      input: [clean_text]
      output:
        stage_output: { type: string }
      graph:
        variables:
          # 合法：外层 retry_count 未被 inner_stage 捕获，子图可独立声明同名私有变量
          retry_count:
            type: integer
            default: 0
        nodes: ...
        edges: ...
```

---

## 二、 显式变量表与 `graph.variables` 的两种声明形态

编译器遵循**先声明后使用（Declaration-Before-Use）**的显式变量表原则：每张图的变量表直接由四类显式 `TypeSpec` 契约源汇总构建——当前图的宿主 `input`、宿主 `output`、图级变量表 `graph.variables`，以及图内未挂载 `for_each` 的 `run` / 内联 `graph` 节点的 `output`。汇总生成图变量表后，编译器即可独立校验图内每个节点的输入与输出类型（符号表构建与类型检查算法详见 [规范：`02-type-system-and-symbols/03-two-phase-symbol-resolution.md`](../../../spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md)）。

在 `graph.variables` 中声明图级局部变量时，每个条目均必须是一个良构的 **`TypeSpec` 映射对象**，并根据是否包含可选的 **`default`** 键区分两种语义：

### 1. 带初值的局部变量声明（包含 `default` 键）

当变量需要在图启动（`start`）时持有初始值（如循环计数器、空列表缓冲、默认配置常量）时，在 `TypeSpec` 中同时指定 `default: <literal>`：

* 编译期立即校验 `default` 初始字面量是否符合该变量声明的 `TypeSpec` 类型契约；
* 该变量在图入口 `start` 处即处于**已赋值（已定值）状态**，无需经过前置节点写入即可被下游节点直接读取；运行期创建栈帧时，会以 `default` 字面量的深拷贝完成初始化。

```yaml
graph:
  variables:
    # 标量带初值声明
    max_retry_limit:
      type: integer
      default: 3
    review_feedback:
      type: string
      default: "首次生成"

    # 空列表与空有序映射带初值声明
    history_logs:
      type: array
      items: { type: string }
      default: []
    task_scores:
      type: object
      additionalProperties: { type: number }
      default: {}
```

### 2. 未初始化的局部变量类型声明（省略 `default` 键）

当图内某局部中间变量仅由 `builtin:*` 算子、`use: workflow:<PackagePath>` 子工作流调用或挂载了 `for_each` 的节点写入，且既不属于宿主 `input` / `output` 也不属于其他未挂载 `for_each` 的 `run` / `graph` 节点的显式 `output` 时，在 `graph.variables` 中仅声明其 `TypeSpec` 而省略 `default` 键（等价于 TypeScript / Rust 中的 `let v: T;`）：

* 该变量在当前图变量表中获得明确的静态类型，可供 `with.output` 或挂载 `for_each` 的节点绑定写入；
* 在图入口 `start` 处处于**未定值状态（`Unbound`）**，必须先由后续节点完成写入定值，方可被下游节点读取（详见下文第四节“编译期全路径必达定值分析”）。

```yaml
graph:
  variables:
    # 仅声明静态类型（省略 default），在 start 处未定值，由后续节点写入后方可读取
    stage_label:
      type: string
    valid_items:
      type: array
      items: { type: string }
```

---

## 三、 宿主输入与 `graph.variables` 互斥规则

为防止外部传入的参数被 `graph.variables` 重复声明或静默覆盖，编译器强制执行**宿主输入与 `graph.variables` 互斥规则**（详见 [规范：`03-static-flow-analysis/02-scoping-and-collision-rules.md`](../../../spec/03-static-flow-analysis/02-scoping-and-collision-rules.md)）：

> 凡是进入当前图时已由宿主环境提供的变量——包括根工作流的顶层 `input` 参数、内联 `graph` 节点的 `input: [...]` 捕获变量，以及挂载在当前内联 `graph` 上的 `for_each` 所注入的 `item` / `key` / `index` 变量——**禁止在当前图的 `graph.variables` 中重复声明类型或初值**。

```yaml
input:
  customer_id: { type: string }
output:
  result: { type: string }

graph:
  variables:
    # 非法（编译期报错）：customer_id 已是顶层 input 变量，禁止在 graph.variables 中重复声明
    customer_id:
      type: string
      default: "default_user"
```

当内联 `graph` 节点需要对外导出变量 `X`（即 `output: { X: <TypeSpec> }`）时，按是否需要读取外层已有值区分两种写法：

```yaml
nodes:
  # 模式 A：子图需要读取外层已定值的 draft，并在其基础上修改后导出
  # -> 将 draft 列入子图 input: [draft]，且不在子图 graph.variables 中重复声明 draft
  refine_existing_draft:
    input: [draft]
    output:
      draft: { type: string }
    graph:
      nodes:
        polish:
          input: [draft]
          output:
            draft: { type: string }
          run:
            executor: builtin
            content: |
              return Output(draft=input.draft.strip())
      edges:
        - start -> polish
        - polish -> end

  # 模式 B：report 由子图首次创建（或完全重写，不读取外层旧值）
  # -> 不将 report 列入子图 input，可在子图 graph.variables 或内部节点中初始化 report
  create_new_report:
    input: [raw_data]
    output:
      report: { type: string }
    graph:
      variables:
        report:
          type: string
          default: "初始报告头"
      nodes:
        append_detail:
          input: [raw_data, report]
          output:
            report: { type: string }
          run:
            executor: builtin
            content: |
              return Output(report=f"{input.report}\n{input.raw_data}")
      edges:
        - start -> append_detail
        - append_detail -> end
```

---

## 四、 编译期全路径必达定值分析（Definite Assignment）

编译器在运行前对控制流图执行分支敏感的全路径必达定值分析（底层数据流方程详见 [规范：`03-static-flow-analysis/03-branch-sensitive-definite-assignment.md`](../../../spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)）：

* 若某节点在 `input`（或 `with.input`）中声明读取变量 `X`，则**从 `start` 到达该节点的每一条可行控制流路径上，`X` 都必须已完成定值**。
* 若多条分支路径在下游汇合，而其中某条分支遗漏了对 `X` 的赋值，编译期静态检查将直接报错。当某条分支仅需为汇合变量赋予常量值、拷贝已有变量或提取结构体字段时，可使用 [`builtin:assign`](../../reference/builtins/assign.md) 完成零开销定值（若需构造或不可变更新 `TypedDict` 结构体，则可使用 [`builtin:object_merge`](../../reference/builtins/object-merge.md)）。

```yaml
nodes:
  route_tier:
    use: builtin:switch
    with:
      input:
        source: user.is_vip

  calc_vip_discount:
    input: [order_total]
    output:
      discount_amount: { type: number }  # 在 .true 路径上通过业务节点计算 discount_amount
    run:
      executor: builtin
      content: |
        return Output(discount_amount=round(input.order_total * 0.1, 2))

  # 在 .false 路径上使用 builtin:assign 零开销将 discount_amount 定值为常量 0.0
  assign_zero_discount:
    use: builtin:assign
    with:
      input:
        source: 0.0
      output:
        value: discount_amount

  # 汇合节点：由于 .true 与 .false 两条路径均已为 discount_amount 赋值，此处读取合法
  apply_payment:
    input: [order_total, discount_amount]
    output:
      final_pay: { type: number }
    run:
      executor: builtin
      content: |
        return Output(final_pay=input.order_total - input.discount_amount)

edges:
  - start -> route_tier
  - route_tier.true -> calc_vip_discount
  - route_tier.false -> assign_zero_discount
  - calc_vip_discount -> apply_payment
  - assign_zero_discount -> apply_payment
  - apply_payment -> end
```

---

## 五、 完整示例：多层栈帧隔离与全路径定值

以下完整工作流展示主图 `variables`、内联子图 `Block Frame` 的显式捕获与同名私有变量隔离，以及多分支汇合前的全路径定值：

```yaml
# 1. 根节点输入与输出契约
input:
  raw_query: { type: string }
  use_cache: { type: boolean }
output:
  final_response: { type: string }

graph:
  # 2. 主图初始状态变量（不能包含顶层 input 中的 raw_query 或 use_cache）
  variables:
    trace_tag:
      type: string
      default: "main_frame"

  nodes:
    # 步骤 1：根据布尔变量 use_cache 路由至两条不同分支
    check_cache_flag:
      use: builtin:switch
      with:
        input:
          source: use_cache

    # 分支 A（.true）：从缓存加载结果，为汇合变量 query_result 赋值
    load_cached_result:
      input: [raw_query]
      output:
        query_result: { type: string }
      run:
        executor: builtin
        content: |
          return Output(query_result=f"[CACHED] {input.raw_query}")

    # 分支 B（.false）：通过内联子图执行多步检索，同样为汇合变量 query_result 赋值
    # 子图仅捕获 raw_query，未捕获主图的 trace_tag
    live_search_subflow:
      input: [raw_query]
      output:
        query_result: { type: string }
      graph:
        # 子图独立声明私有变量 trace_tag，与主图的 trace_tag 完全隔离
        variables:
          trace_tag:
            type: string
            default: "sub_frame_search"
        nodes:
          rewrite_query:
            input: [raw_query, trace_tag]
            output:
              expanded_query: { type: string }
            run:
              executor: builtin
              content: |
                return Output(expanded_query=f"[{input.trace_tag}] {input.raw_query}")

          execute_search:
            input: [expanded_query]
            output:
              query_result: { type: string }
            run:
              executor: builtin
              content: |
                return Output(query_result=f"Search results for {input.expanded_query}")
        edges:
          - start -> rewrite_query
          - rewrite_query -> execute_search
          - execute_search -> end

    # 步骤 2（汇合节点）：无论经过 .true 还是 .false 分支，query_result 均已完成定值
    format_output:
      input: [query_result, trace_tag]
      output:
        final_response: { type: string }
      run:
        executor: builtin
        content: |
          return Output(final_response=f"[{input.trace_tag}] {input.query_result}")

  # 3. 控制流边：两条分支均产出 query_result 后汇合至 format_output
  edges:
    - start -> check_cache_flag
    - check_cache_flag.true -> load_cached_result
    - check_cache_flag.false -> live_search_subflow
    - load_cached_result -> format_output
    - live_search_subflow -> format_output
    - format_output -> end
```

---

下一节：[02. 执行护栏：`max_steps`、调用栈深度与 `timeout_ms`](./02-guardrails-and-timeouts.md)。
