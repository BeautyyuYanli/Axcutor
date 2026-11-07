# RFC-0012: 结构化 `run` 执行器分派机制、移除自然语言变量正则提取与 `builtin` 可信 Python 执行器规范

* **状态**：Proposed
* **所属分类**：执行器 ABI 架构演进、语法正交化与静态类型代码生成（Executor Architecture, Grammar Simplification & Static Type Codegen）
* **前置关联提案**：
  * [`RFC-0011: Axcutor 编译器前后端与 Temporal 运行时参考实现架构`](./0011-compiler-and-temporal-runtime-architecture.md)
* **核心解决的问题**：
  1. **废除自然语言变量正则提取（`RunVarRef`）**：移除前期针对提示词字符串设计的 `\bvariables\.[a-zA-Z_]...` 正则扫描、归一化 AST 中的 `references: Vec<FieldPath>` 字段及 `symbol.capture` 检查，将 `run` 节点的静态数据流依赖完全收敛至节点显式声明的 `input` 契约，彻底解除编译器与执行器内部载荷文本的耦合；
  2. **引入内聚式 `RunSpec` 标记联合体（Tagged Union）与语言无关的 `input_schema` 导出**：将 `run` 字段的值从裸字符串重构为以 `executor` 为必填鉴别键的结构化映射对象（无裸字符串语法糖）；Rust 编译器前端（`axcutor-core`）保持对具体宿主框架（如 Pydantic 或未来 TS 沙箱）的零耦合，仅负责校验 `RunSpec` 结构并在归一化 `Run` 节点中附带其 `input` 变量的 `TypeSpec` 签名（`input_schema`）；
  3. **确立 `RunActivities.execute_run` 按 `executor` 统一分派机制与首个标准执行器 `builtin`**：在 Python Temporal Activity 外壳（`RunActivities.execute_run`）中统一复用超时计时（`deadline`）、心跳取消（`heartbeat` + `AbortSignal`）与出口处的 Rust `validate_run_contract` 契约校验，内部按 `run_spec["executor"]` 分派至内置的 `builtin`（可信进程内 Python 执行器）及未来扩展的其他执行器（如 `ts_sandbox`），同时将 `options.task_queues` 改为按 `executor` 名称路由；
  4. **确立 `builtin` 执行器基于 `TypeSpec` 的前后置 `.py` 模板合成与分支联合返回类型（Union of Branch Output Types）**：通过全局禁用 Python 保留字作为标识符，由 Python `builtin` 执行器模块根据节点的 `input_schema`、`output_schema` 与 `branches_contract` 将 `run.content` 自动合成为包含强类型只读 `Input`（兼容运行时精确 `Decimal` 数值传输）、按分支精细化定义的 `Branch.<branch_id>`（或无分支时的 `Output`）模型及 `async def __axcutor_run__(input: Input) -> ReturnType:` 签名的完整可独立编译与静态分析（`pyright` / `mypy`）的 `.py` 单元。

---

## 一、 问题背景与架构演进动机

### 问题 12.1：自然语言变量正则提取（`RunVarRef`）属于前期过度设计，破坏了编译器与执行器的职责边界

在前期规范与 `axcutor-core` 实现中，编译器假定所有 `run` 载荷均为自然语言提示词字符串，并使用正则表达式 `\bvariables\.([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)` 从文本中提取形如 `variables.user.name` 的引用存入 `references: Vec<FieldPath>`，强制对其执行 `symbol.capture` 白名单校验与静态字段路径类型推导 `TypeOf`。
* **冗余性与侵入性缺陷**：
  1. **静态数据流分析本就不依赖 `RunVarRef`**：在必达定值分析中，节点 $N$ 的读取变量集 $Use(N)$ 完全由节点显式声明的 `input: [...]` 列表唯一决定；在运行期，调度器也严格按 `input: [...]` 裁剪 `input_snapshot`，未在 `input` 中声明的图变量物理上绝不会进入 `run` 执行器。
  2. **阻塞代码类执行器**：当 `run` 承载的是 Python 源码或未来的 TypeScript 代码时，用正则在源码字符串或注释中扫描 `variables.xxx` 既不完备又会产生误报。

### 问题 12.2：`run` 缺乏显式执行器分派结构（`executor`），且外部手工注册表导致工作流无法自包含

在前期设计中，`RunSpec` 被定义为裸字符串 `String`，而在 Temporal 运行时（`axcutor_runtime`）中则要求宿主在启动 `create_worker(client, task_queue, executors)` 时手工传入一个外部函数字典 `executors: Mapping[str, Executor]`，通过 `run` 字符串去查找外部 Python 函数，并将整个 `run` 字符串作为 `options.task_queues` 的路由键。
* 若要让工作流自身能够直接内联携带可执行代码，并支持多种执行器（首先是直接执行可信 Python 代码的 `builtin` 执行器，以及未来可能扩展的 `ts_sandbox` 等执行后端），必须提供一个显式的执行器选择字段 `executor`；
* 将 `executor` 内聚在 `run` 映射对象内部（即 `run: { executor: "<id>", ... }`），而不引入裸字符串简写语法糖，能够保持节点外层字段（`input`、`output`、`branches`、`for_each`、`timeout_ms`、`retry`、`catch`）100% 归属于图调度器，使 `RunSpec` 成为纯粹的标记联合体（Tagged Union）。

### 问题 12.3：`builtin` Python 执行器需要兼顾“零隔离原生性能”、“前后端职责解耦”与“编译期全静态类型安全”

作为第一个标准内置执行器，`builtin` 的定位是**直接在宿主进程内运行可信 Python 代码，无需子进程、容器或受限字节码隔离**。
* 若直接向代码塞入裸 `dict` 并要求返回裸 `dict`，用户在编写代码时完全丧失静态类型检查器（`pyright` / `mypy`）的字段补全与类型校验；且当节点包含多个 `branches` 并关联不同的 `output` 字段子集时，裸字典或单一 `Output` 类无法在类型层面约束“选择某分支时必须且只能提供该分支承诺产出的字段子集”。
* 同时，在跨端架构下，Rust 编译器前端（`axcutor-core`）需要编译为 WASM 在浏览器运行，不应与 Python Pydantic 或未来 TypeScript 的具体模板代码字符串生成耦合；最佳职责分界是由 Rust 编译器前端在归一化 AST 中导出语言无关的节点输入/输出/分支 `TypeSpec` 签名，再由 Python 侧的 `builtin` 执行器模块负责合成完整的 `.py` 模块源码以供静态检查与运行期求值。

---

## 二、 核心设计决策

### 决策 1：彻底移除 `RunVarRef` 自然语言变量正则提取与 `references` 字段

1. **编译器只认节点 `input` 契约**：
   * 图内 `ActionNode` 的静态读取集唯一由 `node.input: List<Identifier>` 确定（顶层 `RootRunNode` 由 `root.input: Map<Identifier, TypeSpec>` 确定）；
   * 未在 `input` 中声明的变量绝不进入执行器的 `input_snapshot`。
2. **静态字段路径（`FieldPathExpr`）与编译器 AST 清理**：
   * 移除 `RunVarRef` 正则提取规则、句尾标点剥离规则，以及 `axcutor-core` 中 `NodeBody::Run` 与 `RootBody::Run` 上的 `references: Vec<FieldPath>` 字段和 `symbol.capture` 检查；
   * 静态字段路径表达式 `FieldPathExpr` 仅用于 DSL 绑定层面的 `for_each.source` 与 `use.with.input.<param>`。

---

### 决策 2：内聚式 `RunSpec` 文法与归一化 AST 中的 `input_schema` 导出

1. **`RunSpec` 标记联合体文法（`run: { executor: "builtin", content: "..." }`）**：
   取消 `run` 的裸字符串形态，将 `RunSpec` 形式化定义为以必填字段 `executor` 为鉴别键的标记联合体（Discriminated Union）：
   ```txt
   RunSpec        ::= BuiltinRunSpec ;

   BuiltinRunSpec ::= {
     executor: "builtin",
     content:  NonEmptyString   (* 满足 |trim(content)| >= 1 的可信 Python 函数体源码 *)
   } ;
   ```
   * **零语法糖原则**：不支持 `run: "..."` 裸字符串简写；`run` 必须为映射对象且显式声明 `executor`。
   * **闭合性约束**：当 `executor == "builtin"` 时，`run` 映射对象必须且只能包含 `executor` 与 `content` 两个键，出现任何其他未知键均在编译期 Pass 1 报 `schema.unknown_field` 错误。
2. **Rust 编译器前端保持语言无关，导出节点 `input_schema`**：
   * `axcutor-core` 不内嵌具体宿主语言（Python Pydantic 或未来 TypeScript）的模板拼接代码；
   * 在 Pass 3 完成符号表查找后，`axcutor-core` 将图内 `NodeBody::Run` 所声明的 `input: Vec<String>` 对应的 `TypeSpec` 子集提取为保序映射 `input_schema: Symbols`（即 $\Sigma_{\text{local}}|_{N.\text{input}}$；对于顶层 `RootRunNode`，其 `root.input` 本身已是完整的 `Symbols`）并保存在归一化 AST 中；
   * 解释器在构造发往 `execute_run` Activity 的请求载荷时，一并传入 `input_schema`、`output_schema` 与 `branches_contract`，使任何语言的执行器均拥有构造强类型输入输出模型所需的完整 `TypeSpec` 元数据。

---

### 决策 3：全局禁用 Python 保留关键字作为标识符（消除属性与参数语法冲突）

为确保所有合法的图变量名、结构体（`TypedDict`）属性名及分支名（`BranchId`）在合成的 Python 代码中均可直接作为点号属性（`input.<var>`、`Branch.<branch>`）与关键字参数（`Output(<field>=...)`）使用，无需引入 `<kw>_` 尾随下划线别名或字典解包特判：

* **扩充全局保留字集合 $\mathcal{R}$**：
  将系统保留字集合 $\mathcal{R}$ 定义为控制流保留字 $\mathcal{R}_{\text{cfg}}$ 与 Python 语言保留关键字集合 $\mathcal{R}_{\text{py}}$（对应 Python 标准库 `keyword.kwlist` 的全部 35 个关键字）的并集：
  $$\mathcal{R} = \mathcal{R}_{\text{cfg}} \cup \mathcal{R}_{\text{py}}$$
  其中：
  $$\mathcal{R}_{\text{cfg}} = \{\texttt{"start"}, \texttt{"end"}, \texttt{"fail"}\}$$
  $$\mathcal{R}_{\text{py}} = \{
    \texttt{"False"}, \texttt{"None"}, \texttt{"True"}, \texttt{"and"}, \texttt{"as"}, \texttt{"assert"}, \texttt{"async"},
    \texttt{"await"}, \texttt{"break"}, \texttt{"class"}, \texttt{"continue"}, \texttt{"def"}, \texttt{"del"}, \texttt{"elif"},
    \texttt{"else"}, \texttt{"except"}, \texttt{"finally"}, \texttt{"for"}, \texttt{"from"}, \texttt{"global"}, \texttt{"if"},
    \texttt{"import"}, \texttt{"in"}, \texttt{"is"}, \texttt{"lambda"}, \texttt{"nonlocal"}, \texttt{"not"}, \texttt{"or"},
    \texttt{"pass"}, \texttt{"raise"}, \texttt{"return"}, \texttt{"try"}, \texttt{"while"}, \texttt{"with"}, \texttt{"yield"}
  \}$$
* **统一受限标识符约束**：
  所有变量名（`input`、`output`、`variables`、`for_each` 的 `item`/`key`/`index`）、`TypedDict` 的 `properties` 属性名，以及 `NodeId`、`BranchId`、`CatchBranchId`，均必须属于 $\text{Identifier} \setminus \mathcal{R}$。

---

### 决策 4：`builtin` 执行器的强类型数据模型（只读 `Input` + 分支联合返回类型 `Union of Branches`）

在 `builtin` 执行器中，用户的 `run.content` 作为异步函数 `async def __axcutor_run__(input: Input) -> ReturnType:` 的函数体运行，遵循以下强类型契约：

1. **只读强类型输入（`input: Input`）**：
   * 函数入参 `input` 是根据该节点的 `input_schema` 生成的只读强类型对象（Pydantic `BaseModel`，配置 `frozen=True, extra="forbid", strict=True`）；
   * 仅包含节点 `input` 显式声明的变量（未声明的外层图变量不在 `Input` 类型中定义，运行期亦不传入）；
   * 若某个输入变量（或其内部嵌套字段）为强类型结构体（`TypedDict`，即 `type: object` + `properties`），则递归映射为嵌套的只读 `BaseModel` 子类，支持全程点号属性访问（如 `input.order_info.subtotal`）；
   * 若为强类型有序映射（`OrderedMap`，即 `type: object` + `additionalProperties`），则映射为 `dict[str, V]`。
2. **输出与控制分支的代数数据类型（ADT）返回模型**：
   * **情形 A：节点声明了非空 `branches`（每个分支对应一个独立的 Output 模型，返回类型为各分支模型的 Union）**：
     * 针对规范化后的每个分支 $b \in \text{dom}(\text{branches\_contract})$，设该分支承诺产出的输出字段子集为 $F_b = \text{branches\_contract}[b].\text{output} \subseteq \text{dom}(\text{output\_schema})$；
     * 为分支 $b$ 生成专属的强类型模型类 `_Branch_<b>(BaseModel)`（挂载于命名空间类 `Branch.<b>` 下），该类**仅包含 $F_b$ 中的字段，且每个字段均为必填**（若 $F_b = \emptyset$，则 `Branch.<b>()` 为零参数构造器）；
     * 函数返回类型注解为所有分支模型构成的判别联合类型（Discriminated Union）：
       $$\text{ReturnType} = \bigcup_{b \in \text{dom}(\text{branches\_contract})} \texttt{\_Branch\_}b$$
     * 用户代码通过 `return Branch.<branch_id>(<field_1>=..., ...)` 同时完成**分支选择**与**该分支专属输出字段的强类型构造**。
   * **情形 B：节点未声明 `branches`（单一 `Output` 模型）**：
     * 当 `branches_contract` 为空时，生成包含 `output_schema` 全部字段的单一强类型模型类 `Output(BaseModel)`（若 `output_schema` 为空 `{}`，则 `Output()` 为零参数构造器），此时 $\text{ReturnType} = \texttt{Output}$；
     * 用户代码直接通过 `return Output(<field_1>=..., ...)` 返回。
3. **嵌套输出结构体的双模兼容（Model 实例与 `TypedDict` 字面量联合注解）**：
   * 当节点的某个 `output` 字段（或深层嵌套属性）为强类型结构体（`TypedDict`）时，代码生成器同时生成：
     1. 确定性命名的 Pydantic 模型类 `Output_<field_path>`（供用户显式实例化 `Output_order_info(order_id=..., total=...)`）；
     2. 同构的 `typing.TypedDict` 类型 `_Dict_Output_<field_path>`；
   * 在父级 `Output` 或 `_Branch_<b>` 模型中，该字段的静态类型注解声明为两者的联合类型 `Output_<field_path> | _Dict_Output_<field_path>`。这样无论用户在 `return` 时传入 `Output_order_info(...)` 实例还是直接传入字典字面量 `{"order_id": ..., "total": ...}`，静态类型检查器（`pyright` / `mypy`）与运行期 Pydantic（自动校验并实例化嵌套字典）均能 **100% 类型安全地通过**。

---

### 决策 5：Python 侧前后置模板合成规范（合成可独立编译与静态类型检查的 `.py` 单元）

`axcutor_runtime` 中的 `builtin` 执行器模块提供纯函数接口，根据节点的 `(input_schema, output_schema, branches_contract, content)` 将自动生成的前置类型头（Preamble）、函数签名包装（Function Header）与统一缩进 4 个空格后的 `run.content` 拼接为一个**完整的、自包含的合法 Python 模块源码字符串**。该合成源码既可在编译/测试工具链中直接交由 `compile()` 与 `pyright` / `mypy` 进行静态检查，又可在 Activity 运行期直接编译执行。

#### 1. `TypeSpec` 到 Python 类型注解的递归映射函数 $\mathcal{T}_{\text{py}}(T)$ 与 `Decimal` 精确数值适配

由于 `axcutor_runtime.codec` 为保持精确 JSON 数值传输将浮点数解码为 `decimal.Decimal`（并在重编码时支持 `float`、`int`、`Decimal`），`TypeSpec` 中的 `type: number` 在生成的类型注解中统一映射为 `float | int | Decimal`：

| `TypeSpec` 构造器形态 | 非空类型映射（$\text{Nullable}(T) = \texttt{false}$） | 可空类型映射（$\text{Nullable}(T) = \texttt{true}$） |
| :--- | :--- | :--- |
| `type: string`（无 `enum`） | `str` | `str \| None` |
| `type: string`（含 `enum: [e_1, ...]`） | `Literal["e_1", ...]` | `Literal["e_1", ...] \| None` |
| `type: integer`（无 `enum`） | `int` | `int \| None` |
| `type: integer`（含 `enum: [e_1, ...]`） | `Literal[e_1, ...]` | `Literal[e_1, ...] \| None` |
| `type: number`（无 `enum`） | `float \| int \| Decimal` | `float \| int \| Decimal \| None` |
| `type: number`（含 `enum: [e_1, ...]`） | `float \| int \| Decimal` *(附加枚举值校验器)* | `float \| int \| Decimal \| None` |
| `type: boolean`（无 `enum`） | `bool` | `bool \| None` |
| `type: boolean`（含 `enum: [e_1, ...]`） | `Literal[True]` / `Literal[False]` | `Literal[...] \| None` |
| `type: array, items: U` | `list[` $\mathcal{T}_{\text{py}}(U)$ `]` | `list[` $\mathcal{T}_{\text{py}}(U)$ `] \| None` |
| `type: object, additionalProperties: V` | `dict[str, ` $\mathcal{T}_{\text{py}}(V)$ `]` | `dict[str, ` $\mathcal{T}_{\text{py}}(V)$ `] \| None` |
| `type: object, properties: {...}` (`TypedDict`) | • 在 `Input` 树中：生成的嵌套类 `Input_<path>`<br>• 在 `Output` 树中：`Output_<path> \| _Dict_Output_<path>` | `<NonNullType> \| None` |

* **严格布尔与数值隔离**：在 Pydantic `strict=True` 下，由于 Python 中 `bool` 是 `int` 的子类（`isinstance(True, int) == True`），代码生成器在 `int` 与 `float | int | Decimal` 字段上统一挂载严格校验器以确保 `bool` 值不会被误判为合法的 `integer` 或 `number`，与 Axcutor `TypeSpec` 的严格标量隔离保持 100% 一致。
* **`TypedDict` 非必填属性（$f \notin \text{required}$）的映射**：
  对于结构体 $T$ 中未列入 `required` 的可选属性 $f$（设属性 Schema 为 $P_f = T.\text{properties}[f]$），根据静态字段路径求值规约（缺失属性读取为 `None`），在生成的 `Input` 模型中声明为默认值是 `None` 的可空字段 $f : \mathcal{T}_{\text{py}}(\text{MakeNullable}(P_f)) = \texttt{None}$（在 `Output` 模型中支持省略或设为非空目标类型值，若属性本身非空而值为 `None` 则在导出字典时自动剥离该可选键以符合 JSON Schema 契约）。

#### 2. 合成 `.py` 模块的完整标准模板示例

以下展示一个包含嵌套输入/输出结构体与多分支的 `ActionNode` 及其自动合成的完整 `.py` 源码：

**YAML 节点定义**：
```yaml
review_order:
  input: [raw_order, tax_rate]
  output:
    order_info:
      type: object
      properties:
        order_id: { type: string }
        total: { type: number }
      required: [order_id, total]
    reject_reason:
      type: string
  branches:
    approved:
      description: "订单审核通过"
      output: [order_info]
    rejected:
      description: "订单金额异常被拒绝"
      output: [reject_reason]
    ignored:
      description: "空订单直接忽略"
      output: []
  run:
    executor: builtin
    content: |
      if not input.raw_order.items:
          return Branch.ignored()
      if input.raw_order.subtotal <= 0:
          return Branch.rejected(reject_reason="Subtotal must be positive")
      total = round(float(input.raw_order.subtotal) * (1 + float(input.tax_rate)), 2)
      return Branch.approved(
          order_info={"order_id": input.raw_order.id, "total": total}
      )
```

**合成的完整 `.py` 源码（可直接交由 `pyright` / `mypy` 静态检查并由 `compile()` 编译执行）**：
```python
# === AUTO-GENERATED AXCUTOR BUILTIN MODULE (NODE: review_order) ===
from __future__ import annotations
from decimal import Decimal
from typing import Literal, TypedDict
from pydantic import BaseModel, ConfigDict, PrivateAttr

_MODEL_CFG = ConfigDict(frozen=True, extra="forbid", strict=True)

# --- 1. Input Models (Read-Only Snapshot) ---
class Input_raw_order(BaseModel):
    model_config = _MODEL_CFG
    id: str
    subtotal: float | int | Decimal
    items: list[str]

class Input(BaseModel):
    model_config = _MODEL_CFG
    raw_order: Input_raw_order
    tax_rate: float | int | Decimal

# --- 2. Output Nested Models & TypedDicts ---
class Output_order_info(BaseModel):
    model_config = _MODEL_CFG
    order_id: str
    total: float | int | Decimal

class _Dict_Output_order_info(TypedDict):
    order_id: str
    total: float | int | Decimal

# --- 3. Branch Variant Models & ReturnType Union ---
class _Branch_approved(BaseModel):
    model_config = _MODEL_CFG
    _axcutor_branch: str = PrivateAttr(default="approved")
    order_info: Output_order_info | _Dict_Output_order_info

class _Branch_rejected(BaseModel):
    model_config = _MODEL_CFG
    _axcutor_branch: str = PrivateAttr(default="rejected")
    reject_reason: str

class _Branch_ignored(BaseModel):
    model_config = _MODEL_CFG
    _axcutor_branch: str = PrivateAttr(default="ignored")

class Branch:
    approved = _Branch_approved
    rejected = _Branch_rejected
    ignored = _Branch_ignored

ReturnType = _Branch_approved | _Branch_rejected | _Branch_ignored

# --- 4. User Execution Entrypoint ---
async def __axcutor_run__(input: Input) -> ReturnType:
    # --- BEGIN USER CONTENT (line_offset = 50) ---
    if not input.raw_order.items:
        return Branch.ignored()
    if input.raw_order.subtotal <= 0:
        return Branch.rejected(reject_reason="Subtotal must be positive")
    total = round(float(input.raw_order.subtotal) * (1 + float(input.tax_rate)), 2)
    return Branch.approved(
        order_info={"order_id": input.raw_order.id, "total": total}
    )
    # --- END USER CONTENT ---
```

**无 `branches` 节点的合成差异（第 3 部分直接生成 `Output`）**：
```python
# 当节点未声明 branches 时，第 3 部分生成单一 Output 类：
class Output(BaseModel):
    model_config = _MODEL_CFG
    _axcutor_branch: None = PrivateAttr(default=None)
    total_amount: float | int | Decimal

ReturnType = Output

async def __axcutor_run__(input: Input) -> ReturnType:
    total = round(float(input.order_info.subtotal) * (1 + float(input.tax_rate)), 2)
    return Output(total_amount=total)
```

#### 3. 诊断行号精确还原（Line Number Mapping）
由于前置模板的生成是确定性的，设 `content` 第一行在合成 `.py` 文件中的起始行号偏移量为 $L_{\text{offset}}$（即 `__axcutor_run__` 函数头所在行号）。无论是 `SyntaxError` / 静态类型检查器诊断，还是运行期堆栈追踪（`Traceback`），定位到合成文件的第 $L$ 行（$L > L_{\text{offset}}$）均精确对应用户 `run.content` 的第 $L - L_{\text{offset}}$ 行。

---

### 决策 6：`RunActivities.execute_run` 按 `executor` 统一分派与运行期协议

在 Temporal 运行时（`axcutor_runtime`）中，`RunActivities.execute_run` 作为所有 `run` 执行器的统一 Activity 生命周期外壳运行：

1. **按 `run_spec["executor"]` 分派与 `task_queues` 路由**：
   * **Task Queue 路由**：解释器在发起 `workflow.execute_activity("execute_run", ...)` 时，以执行器类型名 `run_spec["executor"]`（如 `"builtin"`）作为键查询 `self._options["task_queues"].get(body["run"]["executor"])`；
   * **内置 `"builtin"` 执行器与可扩展分派表**：`RunActivities` 默认内置 `"builtin"` 执行器处理器（同时允许通过可选的 `executors` 字典扩展或注册未来的其他执行器类型如 `"ts_sandbox"`），因此 `create_worker(client, task_queue)` 开箱即用，无需外部必传手工函数注册表；
   * **共享外层 ABI 守护管线**：无论分派给 `"builtin"` 还是未来扩展的 `"ts_sandbox"`，外层 `RunActivities.execute_run` 均统一管理单次尝试期限（`deadline`）、心跳保活（`heartbeat`）、取消监听与 `AbortSignal` 置位、退出时的 `cancel_and_drain` 清理，以及出口处的 `axcutor_core.validate_run_contract` + Workflow 二次契约校验。
2. **`builtin` 执行器的输入构造（Copy-in）**：
   * `builtin` 执行器根据请求中的 `(input_schema, output_schema, branches_contract, run_spec["content"])` 合成模块并编译执行（可按确定性摘要缓存编译后的模块构建器），调用 `Input.model_validate(request["input_snapshot"])` 构造深层不可变（`frozen=True`）的 `Input` 实例，传入 `await __axcutor_run__(input_instance)`。
3. **输出拆包与契约违约映射（Copy-out & `contract` Exception）**：
   * 函数返回 `res` 后：
     * 若 `res` 不是当前节点生成的 `ReturnType`（即对应的 `Output` 或 `_Branch_<b>` 实例，例如用户漏写 `return` 返回了 `None`，或返回了其他对象），或者用户在构造 `Output(...)` / `Branch.<b>(...)` 时因类型不匹配、缺失必填字段、传入额外字段而在函数体内抛出了 `pydantic.ValidationError`：
     * 执行器统一将其捕获并抛出 `ContractError`（由 `RunActivities.execute_run` 映射为 `ApplicationError(..., type="contract")`），与标准 ABI 的边界契约校验语义严格对齐。
   * 若 `res` 为合法的 `ReturnType` 实例，执行器提取 `branch = res._axcutor_branch` 与 `output = res.model_dump(mode="python")`，组装为 `{"branch": branch, "output": output}` 返回给 `RunActivities.execute_run` 继续完成 `codec.dumps` 与 `axcutor_core.validate_run_contract` 校验。
4. **业务运行期故障映射（`execution` Exception）**：
   * 若 `__axcutor_run__` 执行期间抛出除 `pydantic.ValidationError`（来自 `Output` / `Branch` 构造器）与 `asyncio.CancelledError` 以外的任何未捕获 Python `Exception`（含 `SyntaxError`、`ZeroDivisionError`、`KeyError`、`ValueError`、网络/文件 I/O 异常等），由 `RunActivities.execute_run` 统一捕获并封装为 `ApplicationError(str(error), type="execution")`，进入节点的 `retry` / `catch` 状态机。
