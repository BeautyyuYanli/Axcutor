# 03. Inline Graph：内联块级子图（SESE 容器）

在同一个工作流文件内部，当我们需要：
1. 将多步串联逻辑封装为一个高内聚的阶段，避免中间临时变量污染主图的 `variables`；
2. 在 **`for_each` 的单次元素迭代内部** 或 **并发组 `[worker_a, worker_b]` 的单个并发分支内部** 编写多步串联、条件分支、循环或异常恢复；
3. 为多个步骤设置共享的超时上限（`timeout_ms`）或原子回滚边界（`catch`）；

我们不需要为此拆分外部文件，只需直接在图内节点上声明内联 **`graph`**。

---

## 一、 图内三种节点执行体对比

在 `graph.nodes` 中，`run`、`use` 与内联 `graph` 是完全平等的三种节点形态：

| 节点形态 | 鉴别字段 | 栈帧与作用域模型 | 输入/输出声明方式 |
| :--- | :--- | :--- | :--- |
| **动作节点** | `run` | 直接在当前图栈帧内执行 | `input: [<var>, ...]`<br>`output: { <var>: <TypeSpec> }` |
| **跨文件调用** | `use` | **函数调用栈帧（Function Frame）**：跨文件完全隔离，通过 `with` 映射 | 契约来自目标工作流或 `builtin:*`，可选配 `with` |
| **内联子图** | `graph` | **块级作用域栈帧（Block Frame）**：单入口单出口（SESE）容器，显式捕获外层变量 | `input: [<var>, ...]`<br>`output: [<var>, ...]`（支持列表简写）或 `{ <var>: <TypeSpec> }` |

---

## 二、 内联 `graph` 的核心特性

### 1. 显式输入捕获（`input: [<var>, ...]`）
* 内联 `graph` 节点无需编写繁琐的 `with` 映射，只需通过 `input: [cart_items, coupon_code]` 列出需要从外层图捕获的顶层变量名。
* **未捕获变量完全隔离**：未列入 `input: [...]` 的外层变量对子图内部完全不可见；捕获的变量以只读快照形式自动成为子图初始 `variables` 的一部分。

### 2. 输出列表简写（`output: [<var>, ...]`）
* 因为子图内部的节点（或子图 `graph.variables`）已经声明了变量的完整 `TypeSpec`，内联 `graph` 节点对外可以直接写 **`output: [final_amount, pricing_breakdown]`**！
* 静态检查器会自动从子图的符号表中提取这两个变量的 `TypeSpec` 作为该内联 `graph` 节点的输出类型，免去内外两层重复书写 JSON Schema 的负担。

### 3. 私有中间变量不外泄
* 子图内部节点产出的中间变量（未列入内联 `graph` 对外 `output` 的变量）仅活在子图自己的块级栈帧内，子图到达 `end` 退出后即销毁，不会污染外层主图的命名空间。

---

## 三、 完整示例：在主图中组合 `use` 与内联 `graph`

```yaml
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
output:
  risk_score: { type: number }
  final_amount: { type: number }
  pricing_breakdown: { type: string }

graph:
  nodes:
    # 1. 跨文件调用外部风控工作流（Function Frame）
    evaluate_risk:
      use: workflow:security/check_customer_risk
      with:
        input:
          uid: customer_id
          strict_mode: true
          scene: { value: "checkout" }
        output:
          score: risk_score

    # 2. 内联块级子图（Block Frame）：显式捕获 cart_items 与 coupon_code，使用 output 列表简写导出结果
    pricing_pipeline:
      input: [cart_items, coupon_code]
      output: [final_amount, pricing_breakdown]
      graph:
        nodes:
          # 子图私有中间变量 base_subtotal 与 promo_deduction 仅存在于子图内
          calc_discounts:
            input: [cart_items, coupon_code]
            output:
              base_subtotal: { type: number }
              promo_deduction: { type: number }
            run: "汇总 variables.cart_items 原价写入 output.base_subtotal，并计算优惠码 variables.coupon_code 抵扣额写入 output.promo_deduction。"

          finalize_price:
            input: [base_subtotal, promo_deduction]
            output:
              final_amount: { type: number }
              pricing_breakdown: { type: string }
            run: "用 variables.base_subtotal 减去 variables.promo_deduction 写入 output.final_amount，生成账单说明写入 output.pricing_breakdown。"

        edges:
          - start -> calc_discounts
          - calc_discounts -> finalize_price
          - finalize_price -> end

  edges:
    - start -> evaluate_risk
    - evaluate_risk -> pricing_pipeline
    - pricing_pipeline -> end
```

---

掌握了三大基础节点形态后，下一章我们将进入条件与循环控制流：[01. Branches：排他条件分支与按需产出](../02-control-flow/01-branches.md)。
