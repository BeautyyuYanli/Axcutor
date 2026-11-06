# 01. Basics：基础概念与最小工作流

Axcutor 使用 YAML 或 JSON 声明工作流的数据契约与控制流图（CFG）。本节介绍 DSL 的核心概念、根节点签名、`run` 节点的广义函数契约以及最基础的顺序工作流。

---

## 一、 万物皆节点（Unified Node Model）

在 Axcutor 中，取消了冗余的顶层 `workflow` 包装壳。**一个独立的工作流文件本身就是一个根节点（Root Node）**，对外暴露明确的函数式签名：

$$\text{Node} : \text{Input} \longrightarrow (\text{Branch}, \text{Output})$$

所有输入与输出变量均采用**标准 JSON Schema 的严格强类型子集（`TypeSpec`）**声明（详见 [TypeSpec 速查表](../../reference/03-typespec-cheatsheet.md)）。

---

## 二、 `run` 的本质：受函数签名约束的任意业务代码

在节点中，`run` 代表执行具体计算或 I/O 动作的执行体。

> [!IMPORTANT]
> **`run` 是任意满足函数签名约束的业务代码，而不局限于提示词或大模型（LLM）调用。**
> 只要一个执行单元能够接收节点 `input` 声明的强类型只读输入参数，并在执行完成后返回符合所选 `branch` 及 `output` 类型契约（`TypeSpec`）的结构化数据，它就可以作为 `run` 的载体——包括本地/远程业务函数、Python/JS 脚本沙箱、微服务 HTTP/RPC 接口、数据库查询与事务操作、规则引擎，或者大模型结构化生成（Structured Outputs）。外围的图调度、类型检查、栈帧隔离与容错机制对所有形态的 `run` 完全通用。

运行时对任何 `run` 节点始终保障两点：
1. **最小权限只读注入**：仅向执行器传入节点在 `input` 中显式声明的变量只读快照，未声明的变量完全不可见。
2. **输出类型与分支契约校验**：执行器返回后，运行时严格校验其产出的 `output` 字段是否符合 `TypeSpec`，校验通过后才原子写回图状态。

---

## 三、 根节点的两种形态：单步 `run` vs. 复合 `graph`

作为独立文件入口的根节点，必须声明 `input` 和 `output`（若无参数则写 `{}`），并在 `run` 与 `graph` 之间二选一：

### 形态 A：顶层直接为 `run`（单步原子工作流）

```yaml
input:
  raw_price:
    type: number
    description: "商品原始标价"
  discount_rate:
    type: number
    description: "折扣系数（0.0 ~ 1.0）"
output:
  final_price:
    type: number
    description: "折后四舍五入价格"

run: "计算 input.raw_price * input.discount_rate 并保留两位小数，写入 output.final_price。"
```

### 形态 B：顶层为 `graph`（多步控制流图工作流）

当需要串联或编排多个步骤时，在顶层声明 `graph`。一张基础的 `graph` 包含：
* **`variables`（可选）**：图级共享变量表。进入顶层 `graph` 时，顶层 `input` 中的所有变量会自动作为初始已定值变量注入 `variables` 表中；你也可以在 `graph.variables` 中声明图内部使用的初始状态变量。
* **`nodes`（必填）**：定义图内的各个子节点。
* **`edges`（必填）**：定义控制令牌从起点 `start` 到终点 `end` 的流转路径。

---

## 四、 图内 `run` 节点的轻量级读写声明

因为图本身已经维护了所有变量的类型表，图内的 `run` 节点**无需重复声明输入变量的 `TypeSpec`**，只需通过变量名列表声明读取项：

* **`input: [<var_name>, ...]`**：列出需要从当前图 `variables` 中读取的顶层变量名。若某个变量是强类型结构体（`type: object` + `properties`），只需在 `input` 中列出根变量名（如 `input: [order_info]`），即可在节点内通过点分路径（如 `variables.order_info.subtotal`）直接读取嵌套字段。
* **`output: { <var_name>: <TypeSpec> }`**：声明节点完成后向当前图 `variables` 新增或更新的变量。当控制流到达 `end` 时，运行时自动从 `variables` 中提取与顶层 `output` 同名的变量作为整個工作流的返回值。

### 完整示例：三步顺序工作流（`start -> a -> b -> c -> end`）

```yaml
input:
  order_payload:
    type: string
  tax_rate:
    type: number
output:
  total_amount:
    type: number
  receipt_text:
    type: string

graph:
  nodes:
    # 步骤 1：解析原始报文，产出结构化对象 order_info
    parse_order:
      input: [order_payload]
      output:
        order_info:
          type: object
          properties:
            order_id: { type: string }
            subtotal: { type: number }
          required: [order_id, subtotal]
      run: "解析报文 variables.order_payload，提取订单号与税前小计写入 output.order_info。"

    # 步骤 2：读取结构体字段 order_info.subtotal 与 tax_rate，计算含税总额 total_amount
    calculate_tax:
      input: [order_info, tax_rate]
      output:
        total_amount: { type: number }
      run: "根据税前金额 variables.order_info.subtotal 与税率 variables.tax_rate 计算含税总额，写入 output.total_amount。"

    # 步骤 3：生成收据文本 receipt_text
    format_receipt:
      input: [order_info, total_amount]
      output:
        receipt_text: { type: string }
      run: "为订单 variables.order_info.order_id 生成含税总额 variables.total_amount 的收据，写入 output.receipt_text。"

  edges:
    - start -> parse_order
    - parse_order -> calculate_tax
    - calculate_tax -> format_receipt
    - format_receipt -> end
```

---

下一节我们将学习如何通过 `use` 调用外部独立工作流，实现跨文件模块化复用：[02. Use：跨文件调用子工作流](./02-use-subworkflows.md)。
