# 01. 图栈帧与命名空间（Stack Frames & Scoping）

在组合使用跨文件子工作流（`use`）、内联子图（`graph`）与集合遍历（`for_each`）时，Axcutor 通过严格的栈帧隔离边界、三步工作队列符号表决议、初值互斥规则与全路径必达定值分析，在编译期排除变量命名冲突与未初始化读取。

---

## 一、 三类栈帧与作用域隔离边界

运行时通过三类栈帧管理变量的生命周期与可见性：

1. **函数调用栈帧（`Function Frame` —— 根工作流与 `use` 调用）**：
   * 每次通过 `use` 调用外部工作流（或 `builtin:*`）时，创建与调用方完全隔离的命名空间。
   * 被调用方不可读取父图的任何变量，仅接收通过 `with.input` 传入的值，并在退出时通过 `with.output` 写回父图。
2. **块级作用域栈帧（`Block Frame` —— 内联 `graph` 节点）**：
   * 内联 `graph` 节点采用**显式按名捕获（Explicit Capture）**：仅列入内联 `graph` 节点 `input: [<var>, ...]` 的外层变量（以及挂载 `for_each` 时自动注入的 `item` / `key` / `index` 与 `state`）会进入子图的初始 `variables` 表。
   * **未捕获变量隔离与同名无冲突**：未列入 `input: [...]` 的外层变量对子图不可见。子图内部可声明与外层未捕获变量同名的私有变量（如 `temp_buffer`、`retry_count`），两者位于不同栈帧，互不覆盖。
3. **单次迭代局部隔离帧（`Iteration Frame` —— 挂载 `for_each` 的 `run` 与 `use` 节点）**：
   * 当 `run`（`ActionNode`）或 `use`（`CallNode`）挂载 `for_each` 时，针对每个元素构造单次迭代局部符号表 $\Sigma_{\text{iter}} = \Sigma_{\text{outer}} \uplus \Sigma_{\text{injected}}$（`item` / `key` / `index` 不可与外层图变量同名）。
   * **挂载 `for_each` 时的 `input` 声明差异**：
     * 在 **`run` 节点**上，`input: [...]` **必须显式列出** `run` 所需读取的 `item` / `key` / `index`、`state` 及外层变量（未列入 `input` 的变量不会进入传给执行器的 `input_snapshot`）；
     * 在 **内联 `graph` 节点**上，由于 `item` / `key` / `index` 与 `state` 已自动注入子图的 `Block Frame`，内联 `graph` 的 `input: [...]` 仅用于捕获外层普通变量，**禁止**重复列出 `item` / `key` / `index` 或 `state`。

```yaml
graph:
  variables:
    # 外层图的私有计数变量
    retry_count: 0

  nodes:
    # 1. Function Frame：通过 with.input / with.output 跨命名空间传值
    external_step:
      use: workflow:common/sanitize_input
      with:
        input:
          raw: request_text
        output:
          cleaned: clean_text

    # 2. Block Frame：仅捕获 clean_text，未捕获外层的 retry_count
    inner_stage:
      input: [clean_text]
      output: [stage_output]
      graph:
        variables:
          # 合法：外层 retry_count 未被 inner_stage 捕获，子图可独立声明同名私有变量
          retry_count: 0
        nodes: ...
        edges: ...
```

---

## 二、 `graph.variables` 初值声明的两种形式与三步符号表决议

编译器采用**三步工作队列不动点决议算法（Worklist Symbol Resolution）**构建图级符号表 $\Sigma_G$：先收集所有显式 `TypeSpec` 并预注册 `graph.variables` 中的孤立标量字面量，再通过工作队列迭代推导 `builtin:*`、`for_each` 与内联子图的 `output: [...]` 列表简写，最后对被节点写入的 `graph.variables` 简写初值执行类型兼容性校验。

在 `graph.variables` 中声明图内初始状态变量时，支持以下两种语法形式：

### 1. 标量字面量简写（`<var_name>: <literal>`）

当初始值为非空基础标量（`string`、`integer`、`number`、`boolean`），或该变量已在当前图内某节点的 `output` 中声明了 `TypeSpec` 时，可直接书写字面量初值。

* **孤立标量预注册**：若某标量简写变量未被图内任何节点写入（只读配置常量），编译器在阶段 1 即将其预注册至符号表，可直接传给 `builtin:switch`、`for_each` 或被内联 `graph` 的 `input` 捕获；
* **同帧类型对齐校验**：若该变量同时也是图内节点的输出目标，编译器在阶段 3 校验该字面量与节点输出 `TypeSpec` 的兼容性。

```yaml
graph:
  variables:
    max_retry_limit: 3          # 孤立标量简写：阶段 1 直接预注册为 integer，可被子图 input 或 builtin 算子直接读取
    is_dry_run: false           # 孤立标量简写：阶段 1 直接预注册为 boolean，可直接用于 builtin:switch
    retry_count: 0              # 若被图内节点写入，则与节点 output 声明的数值类型执行兼容性对齐
    review_feedback: "首次生成"  # 自动推导为 string
```

### 2. 显式带类型声明（`<var_name>: { <TypeSpec>, default: <literal> }`）

当初始值为空数组 `[]`、空字典 `{}`，或需要在无节点 `output` 声明的情况下指定带 `enum` 约束或可空类型 `[T, "null"]` 的初值时，使用完整 `TypeSpec` 加 `default` 字段：

```yaml
graph:
  variables:
    # 空列表必须显式声明 items 类型与 default: []
    history_logs:
      type: array
      items: { type: string }
      default: []

    # 空有序映射必须显式声明 additionalProperties 类型与 default: {}
    task_scores:
      type: object
      additionalProperties: { type: number }
      default: {}
```

---

## 三、 宿主输入与 `graph.variables` 初值互斥规则

为防止外部传入的参数值被 `graph.variables` 中的默认初值静默覆盖，编译器强制执行**宿主输入与 `graph.variables` 初值互斥规则**：

> 凡是进入当前图时已由宿主环境提供的变量——包括根工作流的顶层 `input` 参数、内联 `graph` 节点的 `input: [...]` 捕获变量，以及挂载在当前内联 `graph` 上的 `for_each` 所注入的 `item` / `key` / `index` 与 `state` 变量——**禁止在当前图的 `graph.variables` 中重复声明初值**。

```yaml
input:
  customer_id: { type: string }
output:
  result: { type: string }

graph:
  variables:
    # 非法（编译期报错）：customer_id 已是顶层 input 变量，禁止在 graph.variables 中重复声明初值
    customer_id: "default_user"
```

当内联 `graph` 节点需要对外导出变量 `X`（即 `output: [X]`）时，按是否需要读取外层已有值区分两种写法：

```yaml
nodes:
  # 模式 A：子图需要读取外层已定值的 draft，并在其基础上修改后导出
  # -> 将 draft 列入子图 input: [draft]，且不在子图 graph.variables 中重复声明 draft
  refine_existing_draft:
    input: [draft]
    output: [draft]
    graph:
      nodes:
        polish:
          input: [draft]
          output:
            draft: { type: string }
          run: "润色已有的 variables.draft 并写回 output.draft。"
      edges:
        - start -> polish
        - polish -> end

  # 模式 B：report 由子图首次创建（或完全重写，不读取外层旧值）
  # -> 不将 report 列入子图 input，可在子图 graph.variables 或内部节点中初始化 report
  create_new_report:
    input: [raw_data]
    output: [report]
    graph:
      variables:
        report: "初始报告头"
      nodes:
        append_detail:
          input: [raw_data, report]
          output:
            report: { type: string }
          run: "基于 variables.raw_data 补充 variables.report，写入 output.report。"
      edges:
        - start -> append_detail
        - append_detail -> end
```

---

## 四、 编译期全路径必达定值分析（Definite Assignment）

编译器在运行前对控制流图执行分支敏感的全路径必达定值分析：

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
    run: "计算 VIP 折扣写入 output.discount_amount。"

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
    run: "计算 variables.order_total - variables.discount_amount 写入 output.final_pay。"

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
    trace_tag: "main_frame"

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
      run: "查询 variables.raw_query 的缓存结果，写入 output.query_result。"

    # 分支 B（.false）：通过内联子图执行多步检索，同样为汇合变量 query_result 赋值
    # 子图仅捕获 raw_query，未捕获主图的 trace_tag
    live_search_subflow:
      input: [raw_query]
      output: [query_result]
      graph:
        # 子图独立声明私有变量 trace_tag，与主图的 trace_tag 完全隔离
        variables:
          trace_tag: "sub_frame_search"
        nodes:
          rewrite_query:
            input: [raw_query, trace_tag]
            output:
              expanded_query: { type: string }
            run: "结合标记 variables.trace_tag 改写检索词 variables.raw_query，写入 output.expanded_query。"

          execute_search:
            input: [expanded_query]
            output:
              query_result: { type: string }
            run: "执行检索 variables.expanded_query，将结果写入 output.query_result。"
        edges:
          - start -> rewrite_query
          - rewrite_query -> execute_search
          - execute_search -> end

    # 步骤 2（汇合节点）：无论经过 .true 还是 .false 分支，query_result 均已完成定值
    format_output:
      input: [query_result, trace_tag]
      output:
        final_response: { type: string }
      run: "附加主图标记 variables.trace_tag 格式化结果 variables.query_result，写入 output.final_response。"

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
