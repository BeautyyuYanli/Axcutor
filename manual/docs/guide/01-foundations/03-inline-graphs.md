# 03. Inline Graph：内联块级子图（SESE 容器）

在单个工作流文件内部，内联 `graph` 节点用于将多步控制流封装为一个单入口单出口（Single-Entry Single-Exit, SESE）的块级作用域。常见适用场景包括：

1. **阶段性作用域隔离**：将多步计算封装为高内聚阶段，防止中间临时变量暴露到外层图的 `variables` 表。
2. **在结构化并发内部编写复杂控制流**：在 `for_each` 的单次元素迭代内，或在 Fork-Join 并发组 `[worker_a, worker_b]` 的单个并发分支内，嵌套多步串联、条件分支或成环回边循环。
3. **多步共享超时与事务回滚边界**：为一组相连步骤配置共享的 `timeout_ms`、`retry` 或 `catch` 原子回滚边界。

---

## 一、 图内三类节点形态对比

在 `graph.nodes` 中，`run`、`use` 与内联 `graph` 是地位对等的三类节点形态：

| 节点形态 | 鉴别字段 | 栈帧与作用域模型 | 输入与输出声明方式 |
| :--- | :--- | :--- | :--- |
| **动作节点** | `run` | 在当前图栈帧内直接执行 | `input: [<var>, ...]`<br>`output: { <var>: <TypeSpec> }` |
| **跨文件调用** | `use` | **函数调用栈帧（`Function Frame`）**：跨文件命名空间完全隔离，通过 `with` 映射 | 契约由目标工作流或 `builtin:*` 定义，可选配 `with` |
| **内联子图** | `graph` | **块级作用域栈帧（`Block Frame`）**：单入口单出口（SESE）容器，按名显式捕获外层变量 | `input: [<var>, ...]`<br>`output: [<var>, ...]`（列表简写）或 `{ <var>: <TypeSpec> }` |

---

## 二、 内联 `graph` 的核心语法规则

### 1. 显式输入捕获（`input: [<var>, ...]`）

内联 `graph` 节点不使用 `with` 映射，而是通过 `input: [<var>, ...]` 显式列出需要从外层图捕获的顶层变量名：

* 列入 `input` 的外层变量以只读快照形式注入子图的初始 `variables` 表。
* 未列入 `input` 的外层变量对子图内部不可见。

```yaml
nodes:
  pricing_subflow:
    # 仅从外层图捕获 cart_items 与 coupon_code；外层其他变量对子图不可见
    input: [cart_items, coupon_code]
    output: [final_amount]
    graph:
      nodes: ...
      edges: ...
```

### 2. 输出列表简写（`output: [<var>, ...]`）

由于子图内部节点（或子图 `graph.variables`）已声明了变量的完整 `TypeSpec`，内联 `graph` 节点的 `output` 支持直接书写变量名列表 `output: [<var_1>, <var_2>]`，由编译器自动从子图符号表中推导输出类型，免去内外重复声明：

```yaml
nodes:
  pricing_subflow:
    input: [cart_items]
    # 列表简写：自动复用子图内部 finalize_price 节点为 final_amount 声明的 { type: number }
    output: [final_amount]
    graph:
      nodes:
        finalize_price:
          input: [cart_items]
          output:
            final_amount: { type: number }
          run: "汇总 variables.cart_items 金额写入 output.final_amount。"
      edges:
        - start -> finalize_price
        - finalize_price -> end
```

### 3. 内部私有中间变量隔离

子图内部节点产出的变量中，只有列入内联 `graph` 节点 `output` 的变量会在子图到达 `end` 时原子写回外层图；其余未导出的中间变量仅存在于子图的 `Block Frame` 内，子图退出后即销毁：

```yaml
nodes:
  pricing_subflow:
    input: [cart_items, coupon_code]
    # 仅导出 final_amount；内部中间变量 base_subtotal 与 promo_deduction 不会进入外层图
    output: [final_amount]
    graph:
      nodes:
        calc_discounts:
          input: [cart_items, coupon_code]
          output:
            base_subtotal: { type: number }    # 子图私有变量
            promo_deduction: { type: number }  # 子图私有变量
          run: "计算原价 output.base_subtotal 与抵扣额 output.promo_deduction。"

        apply_deduction:
          input: [base_subtotal, promo_deduction]
          output:
            final_amount: { type: number }     # 导出至外层图的目标变量
          run: "计算 variables.base_subtotal - variables.promo_deduction 写入 output.final_amount。"
      edges:
        - start -> calc_discounts
        - calc_discounts -> apply_deduction
        - apply_deduction -> end
```

---

## 三、 完整示例：在主图中组合 `use` 与内联 `graph`

以下完整工作流展示如何在顶层主图中先通过 `use` 调用外部风控工作流，再通过内联 `graph` 封装两步计价子流程：

```yaml
# 1. 根节点输入契约
input:
  customer_id: { type: string }
  cart_items:
    type: array
    items:
      type: object
      properties:
        sku: { type: string }
        unit_price: { type: number }
        quantity: { type: integer }
      required: [sku, unit_price, quantity]
  coupon_code: { type: string }

# 2. 根节点输出契约
output:
  risk_score: { type: number }
  final_amount: { type: number }
  pricing_breakdown: { type: string }

graph:
  nodes:
    # 阶段 1：跨文件调用外部风控子工作流（Function Frame）
    # 通过 with.input 传入参数，并将返回字段 score 映射为当前图变量 risk_score
    evaluate_risk:
      use: workflow:security/check_customer_risk
      with:
        input:
          uid: customer_id
          strict_mode: true
          scene: { value: "checkout" }
        output:
          score: risk_score

    # 阶段 2：内联块级子图（Block Frame）
    # - input 显式捕获外层 cart_items 与 coupon_code（customer_id 与 risk_score 对子图不可见）
    # - output 采用列表简写导出 final_amount 与 pricing_breakdown
    pricing_pipeline:
      input: [cart_items, coupon_code]
      output: [final_amount, pricing_breakdown]
      graph:
        nodes:
          # 子图步骤 2.1：计算原价小计与优惠抵扣额（这两个中间变量仅保留在子图栈帧内）
          calc_discounts:
            input: [cart_items, coupon_code]
            output:
              base_subtotal: { type: number }
              promo_deduction: { type: number }
            run: "汇总 variables.cart_items 原价写入 output.base_subtotal，并计算优惠码 variables.coupon_code 抵扣额写入 output.promo_deduction。"

          # 子图步骤 2.2：读取子图私有变量，产出对外导出的 final_amount 与 pricing_breakdown
          finalize_price:
            input: [base_subtotal, promo_deduction]
            output:
              final_amount: { type: number }
              pricing_breakdown: { type: string }
            run: "用 variables.base_subtotal 减去 variables.promo_deduction 写入 output.final_amount，生成账单说明写入 output.pricing_breakdown。"

        # 子图内部控制流：到达子图 end 时，仅将 [final_amount, pricing_breakdown] 提交回主图
        edges:
          - start -> calc_discounts
          - calc_discounts -> finalize_price
          - finalize_price -> end

  # 主图控制流：串联外部调用节点与内联子图节点
  edges:
    - start -> evaluate_risk
    - evaluate_risk -> pricing_pipeline
    - pricing_pipeline -> end
```

---

下一节：[01. Branches：排他条件分支与按需产出](../02-control-flow/01-branches.md)。
