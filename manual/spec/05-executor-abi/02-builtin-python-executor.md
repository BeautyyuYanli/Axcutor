# 02. 内置受信任 Python 执行器（`executor: "builtin"`）形式化规范

本规范定义 Axcutor 标准内置业务执行器 `run: { executor: "builtin", content: <python_body> }` 的执行模型、`TypeSpec` 到 Python / Pydantic v2 强类型模型的映射规则、虚拟 `.py` 模块确定性合成模板，以及运行期异常与行号映射契约。

---

## 一、 执行定位与安全边界

1. **进程内受信任异步执行（Trusted In-Process Execution）**：
   * `builtin` 执行器在运行时 Worker 宿主进程内直接编译并异步执行由节点契约包裹的 Python 函数 `async def execute(input: Input) -> ReturnType`，共享宿主的 Python 运行时与第三方库依赖（如 `httpx`、`openai`、内部 SDK 或跨语言沙箱客户端），不启用语言级受限沙箱。
2. **快照隔离与强类型契约双重保障**：
   * **输入只读隔离**：传入 `execute(input)` 的 `input` 实例基于深拷贝快照构造，且所有生成的 `Input` 及其嵌套 `TypedDict` 模型均启用 `ConfigDict(extra="forbid", frozen=True)`；
   * **输出构造期即校验**：业务代码必须返回当前节点生成的 `Output(...)` 或 `Branch.<branch_id>(...)` 实例，在实例构造与返回边界同步完成 `TypeSpec` 与分支子集契约校验。

---

## 二、 `TypeSpec` 到 Python / Pydantic 类型映射规则

给定编译器前端导出的归一化 JSON Schema `TypeSpec`（`input_schema`、`output_schema` 与 `branches_contract`），`builtin` 执行器按以下规则生成强类型注解与 Pydantic v2 模型：

### 1. 基础标量与容器类型映射

| 归一化 `TypeSpec` | `Input` 侧 Python 类型注解 | `Output` / `Branch` 侧 Python 类型注解 | 边界校验与转换语义 |
| :--- | :--- | :--- | :--- |
| `type: "string"`（无 `enum`） | `StrictStr` (`str`) | `StrictStr` (`str`) | 严格要求 `str`，拒绝数值或布尔隐式转字符串。 |
| `type: "integer"`（无 `enum`） | `StrictInt` (`int`) | `StrictInt` (`int`) | 严格要求 `int`（拒绝 `bool` 与 `float`）。 |
| `type: "number"`（无 `enum`） | `float`（经 `_coerce_input_float` 转换） | `Annotated[float \| int, BeforeValidator(_validate_output_number)]` | **非对称浮点边界（屏蔽传输层 `Decimal`）**：<br>• `Input` 侧将运行时 JSON 传输层的 `Decimal` / `int` / `float` 统一转换为 Python 原生 `float`，使 `input.<field>` 恒为 `float`；<br>• `Output` 侧静态类型注解与运行时校验同时接受原生 `float` 与 `int`（兼容 `Decimal`），拒绝 `bool` 与非有限数 `NaN` / `Inf`。 |
| `type: "boolean"`（无 `enum`） | `StrictBool` (`bool`) | `StrictBool` (`bool`) | 严格要求 `bool`，拒绝 `0` / `1` 或 `"true"`。 |
| 带 `enum: [v_1, ..., v_k]` 的标量 | `Literal[v_1, ..., v_k]` | `Literal[v_1, ..., v_k]` | 严格匹配枚举字面量（区分 `bool` 与 `int`）。 |
| 可空类型 `type: [b, "null"]` | `<MappedType> \| None` | `<MappedType> \| None` | 允许 `None`；但在顶层 `input`、`output` 或必填结构体属性中仍必须显式传参，不自动默认省略为 `None`。 |
| `type: "array"`, `items: T` | `list[<Mapped_T>]` | `list[<Mapped_T>]` | 递归校验每个列表元素。 |
| `OrderedMap`（`type: "object"`, `additionalProperties: V`） | `dict[StrictStr, <Mapped_V>]` | `dict[StrictStr, <Mapped_V>]` | 保持键插入顺序，递归校验每个映射值。 |

### 2. 强类型闭合结构体（`TypedDict`）的输入与输出映射

对于 `type: "object"` 且声明了 `properties` 的闭合结构体（路径记为 `<path>`，如 `user` 或 `user_address`）：

1. **`Input` 侧嵌套结构体模型（`Input_<path>`）**：
   * 生成继承自 `_FrozenModel`（`ConfigDict(extra="forbid", frozen=True)`）的 Pydantic 类 `Input_<path>`；
   * **必填属性**（$f \in \text{set}(\text{required})$）：生成无默认值的必填字段 `f: <MappedType>`；
   * **非必填属性**（$f \notin \text{set}(\text{required})$）：遵循 [`04-static-field-projection-typing.md`](../02-type-system-and-symbols/04-static-field-projection-typing.md) 的缺失非必填属性读取规约，生成带默认值 `= None` 的可空字段 `f: <MappedType> | None = None`，使 Python 代码可直接通过 `input.user.nickname` 安全访问。
2. **`Output` / `Branch` 侧嵌套结构体模型（`Output_<path>`）**：
   * 生成继承自 `_StrictModel`（`ConfigDict(extra="forbid", frozen=True, strict=True)`）的 Pydantic 类 `Output_<path>`，并将字段注解设为 `Output_<path>`，业务代码必须显式构造 `Output_<path>(...)` 实例（不接受原生字典字面量 `{...}`）；
   * **非必填属性的缺省哨兵与键可空正交性**：对于非必填属性 $f \notin \text{set}(\text{required})$，`Output_<path>` 中将其默认值设为内部未设置哨兵 `_UNSET`（若业务代码显式传入值则必须符合其声明的 `<MappedType>`，若 `<MappedType>` 非空则拒绝显式传入 `None`）；在序列化回运行时 JSON 载荷时，所有值仍为 `_UNSET` 的非必填属性自动从输出字典中剥离（等价于 `exclude_unset=True`），严格保持“键缺省”与“值为 `null`”的正交语义。

---

## 三、 顶层 `Input`、`Output` 与 `Branch` 模型合成规则

### 1. 顶层输入模型 `Input`
* 按节点 `input_schema` 的字段顺序生成继承自 `_FrozenModel` 的 `Input` 类；
* 当 `input_schema` 为空映射 `{}` 时，生成空冻结模型 `class Input(_FrozenModel): pass`。

### 2. 无分支节点（`branches_contract == {}`）的输出模型 `Output`
* 生成继承自 `_StrictModel` 并携带类属性 `_branch: ClassVar[str | None] = None` 的顶层 `Output` 类，包含 `output_schema` 中的全部顶层字段（顶层输出字段全部必填，无默认值；当 `output_schema` 为空 `{}` 时，`Output()` 为零参数构造器）；
* **返回类型 `ReturnType`**：`type ReturnType = Output`，业务代码必须显式返回 `Output(...)` 实例。

### 3. 多分支节点（`branches_contract != {}`）的命名空间 `Branch`
* 生成命名空间类 `Branch`，针对 `branches_contract` 中的每个分支 $b \in \text{dom}(\text{branches\_contract})$，在其内部生成继承自 `_StrictModel` 的子类 `Branch.<b>`：
  * 固定携带类属性 `_branch: ClassVar[str] = "<b>"`；
  * **分支输出子集精确裁剪**：`Branch.<b>` 仅包含该分支契约声明的 `output_subset` 字段（即 $f \in \text{set}(\text{branches\_contract}[b].\text{output})$），在构造期静态与动态阻止向当前分支传入不属于该分支承诺产出的多余顶层字段；
* **返回类型 `ReturnType`**：
  $$\texttt{type ReturnType = Branch.}b_1 \texttt{ | Branch.}b_2 \texttt{ | } \dots \texttt{ | Branch.}b_k$$
  业务代码必须显式返回某一个分支构造器实例 `return Branch.<b_i>(...)`。

---

## 四、 虚拟 `.py` 模块确定性合成与源码行号映射

编译器前端或运行时加载器可根据节点的 `(input_schema, output_schema, branches_contract, run.content)` 纯函数式地确定性合成完整的合法 Python 模块源码（供 IDE / Type Checker 静态检查或运行期 `compile()` 执行）：

1. **缩进归一化与函数体挂载**：
   * 对 `run.content` 先剥离首尾空行并执行公共前导空白去缩进（`textwrap.dedent`），再对每个非空行统一添加 4 个空格缩进，嵌入顶层异步入口函数：
     ```python
     async def execute(input: Input) -> ReturnType:
         <indented_run_content>
     ```
2. **确定性行号偏移（`line_offset`）与异常堆栈还原**：
   * 记合成的 `.py` 模块中 `async def execute(input: Input) -> ReturnType:` 所在的行号为 $L_{\text{def}}$（1-based），则行号偏移量定义为 $\text{line\_offset} = L_{\text{def}}$；
   * `run.content` 去首尾空行后的第 $k$ 行（$k \ge 1$）在合成模块中的绝对行号恒为 $L_{\text{py}} = \text{line\_offset} + k$；
   * 当运行期捕获来自合成模块的异常堆栈帧时，通过逆映射 $k = L_{\text{py}} - \text{line\_offset}$ 即可无损还原为用户在 YAML `run.content` 中编写的相对行号。

---

## 五、 运行期执行生命周期与异常分类映射

当运行时激活 `executor: "builtin"` 的单次尝试时，按以下顺序执行并映射结果：

1. **输入快照模型化**：将只读快照 `input_snapshot` 传入 `Input.model_validate(input_snapshot)`（内部将 JSON 传输层的 `Decimal` 转换为原生 `float`），得到冻结的 `input` 实例。
2. **协程求值与取消监听**：在事件循环中启动 `await execute(input)`，并绑定当前尝试的 `abort_signal`（当超时或并发取消触发时向该协程注入 `asyncio.CancelledError`）。
3. **返回结果与异常的 ABI 分类映射**：
   * **正常返回合规实例**：若 `execute(input)` 返回合法的 `Output` 或 `Branch.<b>` 实例，提取 `branch = result._branch` 与剥离 `_UNSET` 后的字典 `output`，封装为标准 `ExecutionSuccess(branch, output)` 交由运行时执行最终边界核验与原子写回；
   * **契约违约映射为 `contract` 异常**：
     * 业务代码在构造 `Output(...)`、`Branch.<b>(...)` 或嵌套 `Output_<path>(...)` 时触发 `pydantic.ValidationError`（如字段缺失、类型不匹配、传入未声明额外字段、向 `number` 传入 `NaN` / `Inf`）；
     * 或 `execute(input)` 返回的对象不属于当前节点定义的 `ReturnType`（例如漏写 `return` 导致返回 `None`，或返回了原生字典字面量 `{...}`）；
     上述情形均在边界被拦截并映射为标准可恢复异常 **`contract`**；
   * **业务与运行时故障映射为 `execution` 异常**：
     * `execute(input)` 执行期间抛出的除上述契约违约与 `CancelledError` 以外的任何未捕获异常（如 `ZeroDivisionError`、`KeyError`、`RuntimeError`、网络 I/O 异常等），或模块加载期的 `SyntaxError`，均携带还原后的 `run.content` 相对行号映射为标准可恢复异常 **`execution`**。
