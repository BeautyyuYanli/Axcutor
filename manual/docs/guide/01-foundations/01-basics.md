# 01. Basics：基础概念与最小工作流

Axcutor 使用 YAML 或 JSON 声明工作流的数据契约与控制流图（CFG）。本节介绍统一节点模型、根节点函数签名、`run` 节点的执行契约以及基础顺序工作流的编写方式。

---

## 一、 统一节点模型（Unified Node Model）

在 Axcutor 中，一个独立的工作流文件本身即为一个根节点（Root Node），对外暴露明确的函数签名：

$$\text{Node} : \text{Input} \longrightarrow (\text{Branch}, \text{Output})$$

所有输入与输出字段均使用标准 JSON Schema 的严格强类型子集（`TypeSpec`）声明（详见 [TypeSpec 速查表](../../reference/03-typespec-cheatsheet.md)）：

```yaml
# 根节点对外契约骨架
input:
  <input_var>: <TypeSpec>   # 必填；若无输入参数则写 input: {}
output:
  <output_var>: <TypeSpec>  # 必填；若无输出参数则写 output: {}

# 执行体：在单步动作 run 与复合控制流图 graph 之间二选一
run:
  executor: builtin
  content: |
    ...
# 或
graph: ...
```

---

## 二、 `run` 的执行契约：受函数签名约束的业务代码

在节点定义中，`run` 表示执行具体计算或 I/O 的最小原子执行体，采用以 `executor` 为鉴别键的结构化映射对象声明。

> [!IMPORTANT]
> **`run` 通过结构化 `executor` 承载受函数签名约束的业务代码。**
> 标准内置执行器 `executor: builtin` 在宿主 Worker 进程内执行受信任的异步 Python 代码（函数体对应 `async def execute(input: Input) -> ReturnType:`），直接通过强类型只读模型 `input.<var>` 读取输入快照（其中 `type: number` 字段自动映射为 Python 原生 `float`），并通过返回 `Output(...)` 或 `Branch.<branch>(...)` 实例完成输出与控制分支路由。此外，宿主也可在 `builtin` 之上或通过独立 `executor` 接入远程沙箱、HTTP/RPC 微服务接口、数据库事务或大模型调用。图调度、静态类型检查、栈帧隔离与容错机制对所有执行器形态保持一致。

运行时对 `run` 节点执行两项隔离与校验保障：

1. **最小权限只读注入**：仅向执行器传入节点在 `input` 中显式声明的变量只读快照（`builtin` 执行器将其封装为 `frozen=True` 的 Pydantic `Input` 实例），未声明的图变量对执行器不可见。
2. **输出类型与分支契约校验**：执行器返回后，运行时（及 `builtin` 的 `Output` / `Branch` 构造器）校验其产出的 `output` 字段是否符合 `TypeSpec`；校验通过后才将结果原子写回所属图栈帧。

```yaml
# 节点只读读取 raw_price 与 discount_rate，校验通过后原子写回 final_price
compute_discount:
  input: [raw_price, discount_rate]
  output:
    final_price: { type: number }
  run:
    executor: builtin
    content: |
      return Output(final_price=input.raw_price * input.discount_rate)
```

---

## 三、 根节点的两种形态：单步 `run` 与复合 `graph`

根节点必须声明顶层 `input` 与 `output`，并在 `run` 与 `graph` 之间二选一。

### 1. 形态 A：顶层为 `run`（单步原子工作流）

当整个工作流仅由单步计算或单次调用组成时，直接在顶层声明 `run`（在 `executor: builtin` 中通过 `input.<input_var>` 读取顶层 `input` 中声明的参数）：

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

run:
  executor: builtin
  content: |
    return Output(final_price=round(input.raw_price * input.discount_rate, 2))
```

### 2. 形态 B：顶层为 `graph`（多步控制流图工作流）

当需要编排多个步骤时，在顶层声明 `graph`。`graph` 由三个核心字段构成：

* **`variables`（可选）**：图级共享局部变量声明表（每个条目均为显式 `TypeSpec` 映射，可选配 `default` 初始值）。进入顶层 `graph` 时，顶层 `input` 中的所有参数自动作为已定值变量注入图级上下文；同时可在 `graph.variables` 中声明图内部使用的带初值状态变量（包含 `default` 键）或未初始化的局部变量类型（省略 `default` 键，由后续节点写入定值）。
  * **同名变量类型一致**：同一图内若在多处（如 `graph.variables`、节点 `output` 或顶层 `output`）对同一变量名声明了 `TypeSpec`，各处的类型定义必须完全一致（跨节点与子工作流传递时的子类型兼容规则见后续章节）。
* **`nodes`（必填）**：定义图内的子节点字典。
* **`edges`（必填）**：定义控制令牌（Control Token）从起点 `start` 到终点 `end` 的流转路径。

```yaml
input:
  request_id: { type: string }
output:
  result_text: { type: string }

graph:
  # 可选：声明图内共享局部变量（顶层 input.request_id 已自动注入图作用域）
  variables:
    # 1. 带初值的局部变量声明（TypeSpec + default）
    step_tag:
      type: string
      default: "v1"
    # 2. 未初始化的局部变量声明（仅声明 TypeSpec，省略 default，由后续节点写入定值）
    normalized_id:
      type: string

  # 必填：声明图内子节点
  nodes:
    normalize_id:
      input: [request_id]
      output:
        normalized_id: { type: string }
      run:
        executor: builtin
        content: |
          return Output(normalized_id=input.request_id.strip().upper())

    build_result:
      input: [normalized_id, step_tag]
      output:
        result_text: { type: string }
      run:
        executor: builtin
        content: |
          return Output(result_text=f"{input.normalized_id}:{input.step_tag}")

  # 必填：声明从 start 到 end 的控制流边
  edges:
    - start -> normalize_id
    - normalize_id -> build_result
    - build_result -> end
```

---

## 四、 图内 `run` 节点的读写声明与结构体字段访问

由于图级符号表已统一维护所有变量的类型，图内 `run` 节点按以下规则声明读写集：

1. **读取声明（`input: [<var_name>, ...]`）**：仅列出需要从当前图 `variables` 中读取的顶层变量名，无需重复声明 `TypeSpec`。
2. **结构体字段访问**：若某变量为强类型结构体（`type: object` + `properties`），只需在 `input` 中列出其**根变量名**（如 `input: [order_info]`），即可在 `builtin` 节点的 Python 代码中通过强类型模型属性（如 `input.order_info.subtotal`）读取嵌套字段。
3. **写入声明（`output: { <var_name>: <TypeSpec> }`）**：声明节点完成后向当前图 `variables` 新增或更新的变量及其类型。当控制流到达 `end` 时，运行时自动从 `variables` 中提取与顶层 `output` 同名的变量作为整个工作流的返回值。

| 声明位置 | 语法形式 | 作用与约束 |
| :--- | :--- | :--- |
| 节点 `input` | `input: [order_info, tax_rate]` | 按变量名引用图内已有变量（只读快照）；结构体只需声明根变量名即可在 `builtin` 中通过 `input.order_info.subtotal` 访问 |
| 节点 `output` | `output: { total_amount: { type: number } }` | 声明写回图内 `variables` 的变量名与 `TypeSpec`；若与已有变量同名，类型必须一致 |

---

## 五、 完整示例：三步顺序工作流（`start -> a -> b -> c -> end`）

以下完整工作流接收原始订单报文与税率，依次执行报文解析、税额计算与收据格式化，并在到达 `end` 时导出最终结果：

```yaml
# 1. 根节点输入契约：进入 graph 时自动注入图级 variables 表
input:
  order_payload:
    type: string
    description: "原始订单 JSON 字符串"
  tax_rate:
    type: number
    description: "适用税率（如 0.08）"

# 2. 根节点输出契约：控制流到达 end 时，自动从图级 variables 中提取同名变量返回
output:
  total_amount:
    type: number
    description: "含税订单总额"
  receipt_text:
    type: string
    description: "格式化后的收据文本"

# 3. 多步控制流图定义
graph:
  nodes:
    # 步骤 1：读取顶层注入的 order_payload，产出内部中间结构体变量 order_info
    parse_order:
      input: [order_payload]
      output:
        order_info:
          type: object
          properties:
            order_id: { type: string }
            subtotal: { type: number }
          required: [order_id, subtotal]
      run:
        executor: builtin
        content: |
          import json
          raw = json.loads(input.order_payload)
          return Output(
              order_info=Output_order_info(
                  order_id=str(raw["order_id"]),
                  subtotal=float(raw["subtotal"]),
              )
          )

    # 步骤 2：读取中间结构体 order_info 与顶层输入 tax_rate，产出顶层输出所需的 total_amount
    calculate_tax:
      input: [order_info, tax_rate]
      output:
        total_amount: { type: number }
      run:
        executor: builtin
        content: |
          total = round(input.order_info.subtotal * (1.0 + input.tax_rate), 2)
          return Output(total_amount=total)

    # 步骤 3：读取 order_info 与上一步产出的 total_amount，产出顶层输出所需的 receipt_text
    format_receipt:
      input: [order_info, total_amount]
      output:
        receipt_text: { type: string }
      run:
        executor: builtin
        content: |
          text = f"Order {input.order_info.order_id}: Total ${input.total_amount:.2f}"
          return Output(receipt_text=text)

  # 4. 顺序控制流边：控制令牌从 start 依次流经三个节点后到达 end
  edges:
    - start -> parse_order
    - parse_order -> calculate_tax
    - calculate_tax -> format_receipt
    - format_receipt -> end
```

---

下一节：[02. Use：跨文件调用子工作流](./02-use-subworkflows.md)。
