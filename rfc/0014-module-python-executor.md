# RFC-0014: 基于预置 Python 模块引用的 `module` 执行器、`__axcutor__.yaml` 包同构布局与契约模板 SHA 校验规范

* **状态**：Implemented
* **所属分类**：执行器 ABI 扩展、工作流包目录结构、静态契约代码生成与运行期契约一致性校验（Executor ABI, Workflow-Package Isomorphism, Static Contract Codegen & Runtime Contract Verification）
* **前置关联提案**：
  * [`RFC-0011: Axcutor 编译器前后端与 Temporal 运行时参考实现架构`](./0011-compiler-and-temporal-runtime-architecture.md)
  * [`RFC-0012: 结构化 run 执行器分派机制、移除自然语言变量正则提取与 builtin 可信 Python 执行器规范`](./0012-run-executor-and-builtin-python.md)
* **核心解决的问题**：
  1. **在不改动现有 `builtin` 内联源码执行器的前提下，新增标准内置执行器 `module`（`run: { executor: "module", content: "<module_spec>" }`）**：支持工作流节点通过相对或绝对 Python 模块名引用宿主环境中预置的 Python 实现模块，避免将完整业务源码内联写入 YAML 字符串或在每次 Activity 尝试时重复执行动态源码拼接与 `exec(compile(...))`；
  2. **确立以 `__axcutor__.yaml` 为入口的“Axcutor Workflow 文件夹 $\equiv$ Python Package”同构目录规范**：将一个包含 `__axcutor__.yaml` 与 `__init__.py` 的文件夹同时定义为 Axcutor 工作流单元与 Python 模块包，允许工作流内的节点通过相对模块路径（如 `.validate_order` 或代表当前包 `__init__.py` 的 `.`）直接绑定同文件夹下的 `.py` 实现与同级生成的 `*_contract.py` 契约模板；
  3. **建立基于契约模板 SHA-256 指纹的运行期防脱节校验机制**：由契约模板生成器计算 `(input_schema, output_schema, branches_contract)` 的确定性 SHA-256 摘要并作为模块级常量 `__axcutor_contract_sha256__` 写入契约模板；`module` 执行器在加载目标业务模块后，通过其导入的契约类型定位所属契约模板模块并读取该 SHA 与当前节点契约 SHA 比对，既确保契约严格同步，又避免在业务模块中引入未使用的导入符号。

---

## 一、 问题背景与设计动机

### 问题 14.1：内联 `builtin` 执行器不适合承载复杂工程化 Python 模块

[`RFC-0012`](./0012-run-executor-and-builtin-python.md) 引入的 `run: { executor: "builtin", content: "<python_body>" }` 适合在 YAML 工作流中直接内联短小的可信 Python 逻辑，但在宿主工程需要维护大量复杂业务节点时存在以下局限：

1. **缺乏标准 Python 模块工程能力**：内联在 YAML 字符串中的 `content` 仅作为合成模块中 `async def execute(input: Input) -> ReturnType:` 的函数体片段存在，不便声明模块顶层导入、辅助函数、类型别名或跨文件相对导入（`from . import ...`）；
2. **调试与测试链路受阻**：`builtin` 在运行期通过 `exec(compile(generated.source, "<axcutor-builtin-...>", "exec"))` 动态编译虚拟文件，IDE 断点调试器（Debugger）、代码覆盖率工具（`coverage`）及静态 Linter 无法直接作用于物理 `.py` 实现文件；
3. **运行期重复合成与编译开销**：对于静态部署在 Worker 进程中的节点逻辑，每次 Activity 尝试均重新拼接 `_PRELUDE` 并动态构建 Pydantic 模型类属于不必要的重复开销。

### 问题 14.2：工作流定义与 Python 模块需要内聚的同级组织结构及防脱节保障

当一个工作流包含多个需要用 Python 实现的 `run` 节点（或整个工作流本身即为一个原子 `run` 节点）时：
* 若将 YAML 与 `.py` 分散在不同目录树中，并在 YAML 中硬编码冗长的全局包路径，会导致工作流在移动、重命名或复用时维护成本高昂；理想的设计是让**一个文件夹同时成为一个 Axcutor Workflow 和一个 Python Package**：其中 `__axcutor__.yaml` 扮演类似 Python `__init__.py` 的工作流包定义角色，工作流内的 `module` 节点直接使用相对于本文件夹的模块名（如 `.node_name`），并在同级就地生成对应的契约模板文件；
* 同时，当工作流 `__axcutor__.yaml` 中的 `input`、`output` 或 `branches` 契约发生变更而开发者未重新生成对应的 Python 契约模板时，运行期必须能够通过契约模板中的 SHA-256 指纹立即检测并拦截契约脱节。

---

## 二、 核心设计决策

### 决策 1：保留 `builtin` 执行器，新增支持相对与绝对模块路径的 `RunSpec::Module`（`executor: "module"`）

1. **`RunSpec` 标记联合体扩展**：
   保留 `executor: "builtin"` 的全部现有语义不变，在 `RunSpec` 中新增一等分支 `ModuleRunSpec`，以 `executor: "module"` 作为鉴别键，使用 `content` 字段承载目标 Python 模块路径（支持相对于当前工作流包的路径或绝对限定名称）：
   ```txt
   RunSpec          ::= BuiltinRunSpec
                      | ModuleRunSpec
                      | ExtensibleExecutorRunSpec ;

   BuiltinRunSpec   ::= {
     executor: "builtin",
     content:  NonEmptyString
   } ;

   ModuleRunSpec    ::= {
     executor: "module",
     content:  ModuleSpec
   } ;

   ModuleSpec       ::= "."                                  (* 当前工作流包的 __init__.py *)
                      | "." ModulePath                       (* 相对于当前工作流包的子模块，如 .charge *)
                      | ModulePath ;                         (* 绝对模块路径，如 my_app.nodes.charge *)

   ModulePath       ::= ModuleIdentifier ( "." ModuleIdentifier )* ;
   ModuleIdentifier ::= Identifier \ R_py ;
   ```
2. **编译期阶段 1（Pass 1）语法与模块名校验**：
   * **闭合性约束**：当 `executor == "module"` 时，`run` 映射对象必须且只能包含 `executor` 与 `content` 两个键；若包含任何其他额外键，编译期报 `schema.unknown_field` 错误；
   * **`ModuleSpec` 格式约束**：
     * 允许 `content` 为单个点号 `"."`（指向当前工作流文件夹自身的 `__init__.py`）；
     * 允许 `content` 以单个前导点号 `.` 开头后接 `ModulePath`（如 `".validate_order"` 或 `".sub.validate_order"`，指向相对于当前工作流文件夹的子模块）；
     * 允许 `content` 为不带前导点号的绝对 `ModulePath`（如 `"app.workflows.order.validate_order"`）；
     * 除单个 `"."` 外，按 `.` 切分后的每个非空路径段均必须满足通用标识符文法 `Identifier`（`[a-zA-Z_][a-zA-Z0-9_]*`）且不得为 Python 硬关键字（$\mathcal{R}_{\text{py}}$）。若 `content` 为空、包含尾随 `.`、连续 `..`、空白字符或非法标识符段，编译期在 `/run/content` 处报 `schema.run` 错误。
3. **AST 表示（`crates/axcutor-core/src/ast/mod.rs`）**：
   ```rust
   pub enum RunSpec {
       Builtin {
           content: String,
       },
       Module {
           content: String,
       },
       Extension {
           executor: String,
           config: Ordered<Value>,
       },
   }
   ```

---

### 决策 2：`__axcutor__.yaml` 工作流包同构目录结构与相对模块解析规则

1. **文件夹即 Axcutor Workflow 与 Python Package 的统一模型**：
   在源码根目录（`source_root`，同时位于 Python `sys.path` 或作为宿主某个根包目录）下，一个工作流由一个同时包含 `__axcutor__.yaml` 与 `__init__.py` 的文件夹表示：
   * `__axcutor__.yaml`：Axcutor 工作流的入口定义文件（地位等同于 Python 包中的 `__init__.py`）；
   * `__init__.py`：Python 包初始化文件（当该工作流为复合图 `graph` 时，可为空或存放本工作流内各节点共享的辅助函数；当该工作流为顶层原子 `run` 且 `content: "."` 时，可直接作为 `async def execute` 的实现文件）。
2. **两类典型工作流文件夹布局**：
   * **情形 A：包含多个 `module` 节点的复合图工作流文件夹（`order_flow/`）**：
     ```txt
     workflows/
     ├── __init__.py
     └── order_flow/                             # 既是 Axcutor Workflow，也是 Python Package order_flow
         ├── __init__.py                         # Python 包入口（可存放本工作流内节点共用的辅助函数）
         ├── __axcutor__.yaml                    # 工作流图定义（内部节点 validate_order 引用 .validate_order）
         ├── validate_order_contract.py          # [自动生成] 节点 validate_order 的契约模板与 SHA
         ├── validate_order.py                   # [人工编写] 节点 validate_order 的业务实现
         ├── charge_card_contract.py             # [自动生成] 节点 charge_card 的契约模板与 SHA
         └── charge_card.py                      # [人工编写] 节点 charge_card 的业务实现
     ```
   * **情形 B：单节点顶层 `run` 的原子工作流文件夹（`double/`）**：
     ```txt
     workflows/
     ├── __init__.py
     └── double/                                 # 既是原子 Axcutor Workflow，也是 Python Package double
         ├── __axcutor__.yaml                    # 顶层 run: { executor: "module", content: "." }
         ├── _contract.py                        # [自动生成] 顶层 run 的契约模板与 SHA
         └── __init__.py                         # [人工编写] from ._contract import ... 并实现 async def execute
     ```
     *（注：顶层 `run` 工作流同样支持将 `content` 写为 `".double"`，此时对应生成 `double_contract.py` 与 `double.py`。）*
3. **从 `workflow_id` 与相对 `content` 到契约模板及目标 Python 模块的映射规则**：
   设某工作流在 `sources` 中的标识路径为 `workflow_id`（如 `"order_flow/__axcutor__.yaml"` 或 `"billing/order_flow/__axcutor__.yaml"`），剥离末尾的 `/__axcutor__.yaml`（或文件后缀）并将 `/` 替换为 `.` 后，得到该工作流相对于 `source_root` 的包路径 `workflow_pkg`（如 `"order_flow"` 或 `"billing.order_flow"`）：

   | `run.content` 形态 | 相对契约模板文件路径（相对于该工作流文件夹） | 相对实现文件路径（相对于该工作流文件夹） | 解析后的目标 Python 模块名（供 `importlib.import_module` 加载） |
   | :--- | :--- | :--- | :--- |
   | `"."` | `_contract.py` | `__init__.py` | `<workflow_pkg>`（如 `"double"`） |
   | `".<name>"`（如 `".charge_card"`） | `<name>_contract.py`（如 `charge_card_contract.py`） | `<name>.py`（如 `charge_card.py`） | `<workflow_pkg>.<name>`（如 `"order_flow.charge_card"`） |
   | `".<sub_pkg>.<name>"` | `<sub_pkg>/<name>_contract.py` | `<sub_pkg>/<name>.py` | `<workflow_pkg>.<sub_pkg>.<name>` |
   | `"<abs_pkg>.<name>"` | 按绝对包路径定位于 `<abs_pkg>/<name>_contract.py` | `<abs_pkg>/<name>.py` | `"<abs_pkg>.<name>"` |

---

### 决策 3：确定性契约 SHA-256 计算与契约模板（Contract Stub）生成规范

1. **节点契约 SHA-256 指纹计算（`compute_contract_sha256`）**：
   给定编译器归一化导出的节点契约三元组 `(input_schema, output_schema, branches_contract)`，定义其规范化载荷为固定键序的 JSON 映射：
   ```json
   {
     "input": <input_schema>,
     "output": <output_schema>,
     "branches": <branches_contract>
   }
   ```
   使用 `axcutor_runtime.codec.dumps` 将上述结构序列化为紧凑、无损、严格保持字段声明顺序的 UTF-8 JSON 字节流，计算其 SHA-256 十六进制小写摘要（64 字符），记为 `contract_sha256`。
2. **契约模板文件生成（`synthesize_contract_module`）**：
   复用 [`RFC-0012`](./0012-run-executor-and-builtin-python.md) 中 `TypeSpec` 到 Pydantic v2 的全部类型映射规则（只读冻结 `Input` / `Input_<path>`、严格校验 `Output` / `Output_<path>` 或 `Branch.<branch_id>` 变体、非对称 `float` 数值边界转换以及可选字段 `_UNSET` 缺省哨兵），生成纯契约 `.py` 模板文件（不包含 `async def execute` 函数体）：
   * 仅在契约模板模块顶层声明单一常量，不在 `Input`、`Output` 或 `Branch` 类上重复挂载哈希属性：
     ```python
     __axcutor_contract_sha256__: str = "<64-hex-chars>"
     ```
   * 在模板模块顶层声明 `__all__`：
     * 无分支节点：`__all__ = ["Input", "Output", "ReturnType", ...]`（含所有嵌套 `Input_<path>` / `Output_<path>` 模型类名）；
     * 多分支节点：`__all__ = ["Input", "Branch", "ReturnType", ...]`。

---

### 决策 4：业务实现模块的编写约定与模板 SHA 反查机制

1. **业务实现模块结构约定**：
   编写者在目标 Python 模块中只需按需从同级契约模板导入实际使用的契约类型（无需导入未使用的 `__axcutor_contract_sha256__` 变量，避免触发 `F401 unused-import` 等 Linter 告警），并实现顶层异步入口函数 `async def execute(input: Input) -> ReturnType:`：
   ```python
   from .review_order_contract import (
       Branch,
       Input,
       Output_order_info,
       ReturnType,
   )

   async def execute(input: Input) -> ReturnType:
       if not input.raw_order.items:
           return Branch.ignored()
       if input.raw_order.subtotal <= 0:
           return Branch.rejected(reject_reason="Subtotal must be positive")
       total = round(input.raw_order.subtotal * (1 + input.tax_rate), 2)
       return Branch.approved(
           order_info=Output_order_info(order_id=input.raw_order.id, total=total)
       )
   ```
2. **通过 `Input.__module__` 反查契约模板 SHA**：
   当 `module` 执行器通过 `importlib.import_module` 加载业务模块 `mod` 后：
   * 读取业务模块中导入的 `input_cls = getattr(mod, "Input", None)`（同时核对无分支节点的 `mod.Output` 或多分支节点的 `mod.Branch` 与 `input_cls` 来自同一契约模块 `input_cls.__module__`）；
   * 通过 `contract_mod = sys.modules.get(input_cls.__module__)` 取得已加载的契约模板模块对象，并读取其模块级常量 `getattr(contract_mod, "__axcutor_contract_sha256__", None)`。

---

### 决策 5：`module` 执行器的运行期协议与异常分类映射

在 `axcutor_runtime` 中，`RunActivities` 默认内置 `"builtin"` 与 `"module"` 两个标准执行器（`self._executors = {"builtin": execute_builtin, "module": execute_module, **(executors or {})}`）。解释器在发起 `execute_run` Activity 请求时，携带当前节点所属的 `workflow_id`（或解析后的目标模块路径）。当激活 `run: { executor: "module", content: "<module_spec>" }` 的单次尝试时，执行器按以下顺序运行：

1. **相对模块路径解析与模块导入**：
   * 若 `content` 以 `.` 开头（`"."` 或 `".<rel_path>"`），结合当前 `workflow_id` 推导出的 `workflow_pkg` 解析出完整模块名（`content == "."` 时直接导入 `workflow_pkg`，否则调用 `importlib.import_module(content, package=workflow_pkg)`）；若 `content` 不以 `.` 开头，直接调用 `importlib.import_module(content)`；
   * 若导入抛出 `ModuleNotFoundError` 或其他导入期异常，映射为标准可恢复异常 **`execution`**；
   * 检查 `getattr(mod, "execute", None)` 是否为协程函数（`inspect.iscoroutinefunction`）；若缺失或不是异步函数，抛出 `ContractError`（映射为 **`contract`** 异常）。
2. **契约模板 SHA 一致性校验（Schema Drift Check）**：
   * 根据当前请求的 `(request["input_schema"], request["output_schema"], request["branches_contract"])` 计算期望哈希 `expected_sha = compute_contract_sha256(...)`；
   * 通过 `mod.Input.__module__` 定位契约模板模块 `contract_mod`（并验证 `mod.Output` 或 `mod.Branch` 归属于同一 `contract_mod`），提取 `actual_sha = getattr(contract_mod, "__axcutor_contract_sha256__", None)`；
   * 若 `mod` 未导入合法契约类型、无法取得 `actual_sha` 或 `actual_sha != expected_sha`，立即抛出 `axcutor_core.ContractError(f"module {target_module!r} contract SHA mismatch: expected {expected_sha}, got {actual_sha}")`，由外层 `RunActivities.execute_run` 映射为标准可恢复异常 **`contract`**。
3. **只读输入构造与协程求值**：
   * 使用契约模块中的 `contract_mod.Input.model_validate(deepcopy(request["input_snapshot"]))` 构造只读 `input_model`（自动完成传输层 `Decimal` 到原生 `float` 的转换与冻结隔离）；
   * 启动 `asyncio.create_task(mod.execute(input_model))` 并与 `signal.wait()` 并发竞速；一旦 `AbortSignal` 置位，立即取消并等待任务排空（`cancel_and_drain`）。
4. **返回值校验、导出与异常归类**：
   * 校验返回值 `result` 必须为契约模块定义的 `contract_mod.Output` 实例（无分支节点）或 `contract_mod.Branch.<branch_id>` 变体实例（多分支节点）；
   * 若返回值类型不符，或在业务函数构造 `Output(...)` / `Branch.<branch_id>(...)` / `Output_<path>(...)` 期间触发了属于该契约模块 `_StrictModel` 子类的 `pydantic.ValidationError`，统一捕获并抛出 `axcutor_core.ContractError`（映射为 **`contract`** 异常）；
   * 若 `mod.execute` 抛出其他未捕获的业务异常，直接保留 Python 原生模块文件名与物理行号堆栈，映射为标准可恢复异常 **`execution`**；
   * 导出 `{"branch": type(result)._branch, "output": _export(result, contract_mod._UNSET)}` 返回给 `RunActivities.execute_run`，继续执行最终的 Rust `axcutor_core.validate_run_contract` 边界核验。

---

## 三、 完整使用示例

### 1. 工作流文件夹结构（`order_flow/`）

```txt
order_flow/
├── __init__.py
├── __axcutor__.yaml
├── summarize_contract.py
└── summarize.py
```

### 2. 工作流定义（`order_flow/__axcutor__.yaml`）

```yaml
input:
  items: { type: array, items: { type: integer } }
output:
  total: { type: integer }

graph:
  nodes:
    summarize:
      input: [items]
      output:
        total: { type: integer }
      run:
        executor: module
        content: .summarize

  edges:
    - start -> summarize
    - summarize -> end
```

### 3. 自动生成的同级契约模板（`order_flow/summarize_contract.py`）

```python
# === AUTO-GENERATED AXCUTOR CONTRACT MODULE ===
...
__axcutor_contract_sha256__: str = "5d41...7c82"

class Input(_ax_FrozenModel):
    items: list[Annotated[StrictInt, BeforeValidator(_ax_integer)]]

class Output(_ax_StrictModel):
    _branch: ClassVar[str | None] = None
    total: StrictInt

ReturnType: TypeAlias = Output

__all__ = ["Input", "Output", "ReturnType"]
```

### 4. 编写者实现的同级业务模块（`order_flow/summarize.py`）

```python
from .summarize_contract import Input, Output, ReturnType


async def execute(input: Input) -> ReturnType:
    return Output(total=sum(input.items))
```
