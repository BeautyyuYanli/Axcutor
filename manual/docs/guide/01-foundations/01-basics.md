# 01. Basics：基础概念与最小工作流

Axcutor 使用 YAML 或 JSON 声明工作流的数据契约与控制流图（CFG）。本节介绍统一节点模型、根节点函数签名、`run` 节点的执行契约（`executor: builtin`、`executor: module` 与 `executor: signal`）以及基础顺序工作流的编写方式。

---

## 一、 统一节点模型（Unified Node Model）

在 Axcutor 中，一个独立的工作流包（包含 `__axcutor__.yaml` 的目录）或单文件入口工作流本身即为一个根节点（Root Node），对外暴露明确的函数签名：

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

## 二、 `run` 的执行契约：受函数签名约束的业务代码与协作式等待

在节点定义中，`run` 表示执行具体计算、外部 I/O 或协作式挂起等待的最小原子执行体，采用以 `executor` 为鉴别键的结构化映射对象声明。

> [!IMPORTANT]
> **`run` 通过结构化 `executor` 承载受函数签名约束的业务执行或协作式等待。**
> Axcutor 内建提供三类保留执行器形态：
> - **`executor: builtin`（内联 Python 执行器）**：在 YAML 的 `content` 中直接编写异步函数体（对应 `async def execute(input: Input) -> ReturnType:`），运行时在内存中动态合成强类型 `Input`、`Output` 与 `Branch` 模型并注入函数作用域，适合轻量级转换、胶水逻辑或单文件快速原型。
> - **`executor: module`（模块级 Python 执行器）**：在具名工作流包（包含 `__axcutor__.yaml` 与 `__init__.py` 的目录）内，通过 `content: "."` 或 `content: ".<local_path>"` 指向包的 `__init__.py` 或独立 `.py` 源文件。每个模块导入由 `axcutor contracts` 预生成的伴生强类型契约桩文件（`*_contract.py` / `_contract.py`）并定义显式 `async def execute(input: Input) -> ReturnType:`，天然获得完整的 IDE 补全、静态类型检查（`pyrefly check`）、单元测试与调试器断点能力，且运行时在边界强制校验编译期契约指纹（`__axcutor_contract_sha256__`）。
> - **`executor: signal`（协作式信号等待执行器）**：以闭合配置 `run: { executor: signal }` 在工作流内部直接挂起等待外部信号，不派发 Activity 任务、不占用 Worker 槽位。外部系统通过 HTTP `POST /v1/executions/{id}:signal` 或 CLI `axcutor signal` 提交满足节点 `output` / `branches` 契约的信号以恢复执行，适用于审批门禁、人工复核或外部异步回调汇聚。
>
> 在 `builtin` 与 `module` 执行器中，业务代码均通过强类型只读模型 `input.<var>` 读取输入快照（其中 `type: number` 字段自动映射为 Python 原生 `float`），并通过返回 `Output(...)` 或 `Branch.<branch>(...)` 实例完成输出与控制分支路由。此外，宿主也可通过扩展 `executor` 接入远程沙箱、HTTP/RPC 微服务接口、数据库事务或大模型调用。图调度、静态类型检查、栈帧隔离与容错机制对所有执行器形态保持一致。

运行时对 `run` 节点执行两项隔离与校验保障：

1. **最小权限只读注入**：仅向执行器传入节点在 `input` 中显式声明的变量只读快照（`builtin` 与 `module` 执行器均将其封装为 `frozen=True` 的 Pydantic `Input` 实例），未声明的图变量对执行器不可见。
2. **输出类型与分支契约校验**：执行器返回（或外部信号到达）后，运行时校验其产出的 `output` 字段与分支是否符合 `TypeSpec` 及 `branches` 契约；校验通过后才将结果原子写回所属图栈帧。

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
2. **结构体字段访问**：若某变量为强类型结构体（`type: object` + `properties`），只需在 `input` 中列出其**根变量名**（如 `input: [order_info]`），即可在 `builtin` 或 `module` 节点的 Python 代码中通过强类型模型属性（如 `input.order_info.subtotal`）读取嵌套字段。
3. **写入声明（`output: { <var_name>: <TypeSpec> }`）**：声明节点完成后向当前图 `variables` 新增或更新的变量及其类型。当控制流到达 `end` 时，运行时自动从 `variables` 中提取与顶层 `output` 同名的变量作为整个工作流的返回值。

| 声明位置 | 语法形式 | 作用与约束 |
| :--- | :--- | :--- |
| 节点 `input` | `input: [order_info, tax_rate]` | 按变量名引用图内已有变量（只读快照）；结构体只需声明根变量名即可在 Python 中通过 `input.order_info.subtotal` 访问 |
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

## 六、 模块级 Python 执行器（`executor: module`）与契约桩代码生成

当工作流以**工作流包（Workflow Package）**目录组织（即目录内包含 `__axcutor__.yaml` 与 `__init__.py`）且节点逻辑包含复杂业务、第三方库依赖或需要独立编写 `pytest` 单元测试时，可将 `executor: builtin` 演进为 `executor: module`，将业务代码拆分为同包内的独立 `.py` 文件。

### 1. 包内相对模块寻址（`LocalModuleSpec`）

`executor: module` 的 `content` 字段仅允许使用以单个点号 `.` 开头的**包内相对模块路径**（禁止跨包绝对导入或 `..` 越界）：

* **局部模块**：写法为 `content: ".<local_path>"`（如 `content: ".parse_order"` 或 `content: ".steps.parse_order"`），运行时结合当前工作流包 ID（如 `billing.checkout`）与 Worker 根映射解析目标。默认同名映射下，`.parse_order` 对应 `billing.checkout.parse_order`。
* **当前包**：顶层 `run` 和图内节点均可使用 `content: "."`，直接指向当前工作流包自身的 `__init__.py`（其伴生契约桩固定为同目录下的 `_contract.py`）。

> [!NOTE]
> 未挂载进工作流包命名空间的独立单文件入口工作流（编译期赋予匿名身份 `"__main__"`）不能直接使用 `executor: module`，否则编译器报错 `module.anonymous_package`。匿名入口仍可使用 `builtin`、扩展执行器及内联图，或调用公开工作流包。

```yaml
# 位于工作流包 billing/checkout/__axcutor__.yaml 内的节点片段
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
    executor: module
    content: ".parse_order"
```

### 2. 伴生契约桩（`*_contract.py`）与实现模块编写

运行 `axcutor contracts workflows/billing/checkout --mount billing=workflows/billing` 后，工具链会根据 `__axcutor__.yaml` 中可达 `module` 节点的静态契约，在目标 `.py` 模块同级目录生成伴生契约桩 `<module>_contract.py`。模板导出 `Input`、`Output` 或 `Branch`、`ReturnType` 及嵌套模型，并包含模块级契约指纹 `__axcutor_contract_sha256__`。业务开发者在 `<module>.py` 中导入契约并实现 `async def execute(input: Input) -> ReturnType:`：

```python
# billing/checkout/parse_order.py
import json
from .parse_order_contract import Input, Output, Output_order_info, ReturnType


async def execute(input: Input) -> ReturnType:
    # 业务解析与生成契约分开维护。
    raw = json.loads(input.order_payload)
    # 嵌套输出结构体须显式使用生成模型。
    return Output(
        order_info=Output_order_info(
            order_id=str(raw["order_id"]),
            subtotal=float(raw["subtotal"]),
        )
    )
```

运行时在每次尝试执行节点前，提取 `mod.Input.__module__` 对应的契约模块并比对 `__axcutor_contract_sha256__` 指纹；若 YAML 契约已变更但未重新生成契约桩、异步入口缺失或契约类型来源不合规，运行时抛出可由 `catch` 捕获的 `contract` 异常。挂载、契约检查与部署边界见 [04. 包挂载与 Python 模块](./04-packages-and-modules.md)。

---

## 七、 协作式信号等待执行器（`executor: signal`）与外部唤醒

当工作流需要在特定节点挂起并等待外部事件（如人工审批、异步回调通知或外部合规放行）时，可将节点声明为闭合配置 `run: { executor: signal }`（不允许携带 `content` 或其他附加字段）：

```yaml
# 挂起等待外部审批信号，24 小时未收到合法信号则转入 expired 分支
await_approval:
  input: [order_info]
  output:
    reviewer: { type: string }
  branches:
    approved:
      description: "审批通过并记录审核人"
      output: [reviewer]
    rejected:
      description: "审批驳回"
      output: []
  timeout_ms: 86400000
  catch:
    expired: [timeout]
  run:
    executor: signal
```

* **等待状态暴露（`Frame.waiting`）**：节点激活后直接在工作流协程内部挂起（不占用 Activity Worker 槽位），并将其规范等待地址（如 `__main__/await_approval`）追加至所属栈帧的 `waiting` 列表中；外部调用方可通过查询活跃栈帧的 `variables` 与 `waiting` 获取上下文及等待地址。
* **外部契约校验与原子认领**：调用方通过 [HTTP `POST /v1/executions/{id}:signal`](../../reference/04-execution-http-api.md#投递外部信号) 或 [CLI `axcutor signal`](../00-run-with-docker.md#使用-cli-恢复等待节点) 提交 `address`、`branch`、`output` 及可选的帧步数护栏 `step`。若分支或输出不符合节点契约，请求以 `422 signal_validation_failed` 被原子拒绝，节点保持原等待状态且不消耗 `retry` 次数；校验通过后才认领槽位、写回 `output` 并沿所选分支继续推进。

---

下一节：[02. Use：工作流包与跨包调用](./02-use-subworkflows.md)。
