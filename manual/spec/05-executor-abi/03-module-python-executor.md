# 03. 基于预置 Python 模块引用的 `module` 执行器与契约模板 SHA 校验规范

本规范定义 Axcutor 标准内置业务执行器 `run: { executor: "module", content: <LocalModuleSpec> }` 的执行模型、`__axcutor__.yaml` 工作流包与 Python Package 同构目录布局、契约模板（Contract Stub）确定性生成与 `contract_sha256` 指纹计算规则，以及运行期模块加载、SHA 防脱节校验与异常分类映射契约。

---

## 一、 执行定位与包内私有模块边界

1. **进程内预置 Python 模块执行（Pre-Packaged Module Execution）**：
   * 与将 Python 函数体源码内联在 YAML 字符串中的 [`executor: "builtin"`](./02-builtin-python-executor.md) 互补，`executor: "module"` 通过包内单点相对路径（`.` 或 `.<LocalPath>`）直接绑定所属工作流包目录下的物理 `.py` 实现文件，并在宿主 Worker 进程内调用其顶层异步入口函数 `async def execute(input: Input) -> ReturnType:`。
   * 物理 `.py` 模块支持完整的标准 Python 工程能力（顶层导入、辅助函数、类型别名、IDE 断点调试、代码覆盖率统计与静态 Linter 检查），且无需在每次 Activity 尝试时重复执行动态源码拼接与 `compile()`。
2. **包与模块身份正交公理及单点包内相对寻址**：
   * **单一文件夹 $\equiv$ 单一工作流包（`__axcutor__.yaml`）$\equiv$ 单一 Python Package（`__init__.py`）**：每个具名工作流包在物理目录树中唯一对应一个同时包含 `__axcutor__.yaml` 与 `__init__.py` 的文件夹，其点分包路径记为 $\text{workflow\_pkg} \in \text{PackagePath}$（如 `billing.charge`）。
   * **包内私有性**：工作流包文件夹内部的 `.py` 实现文件与 `*_contract.py` 契约模板严格归属于该工作流包私有，不构成独立的 Axcutor 工作流，不可被 `use: workflow:...` 跨包寻址。
   * **仅允许单点包内相对路径（`LocalModuleSpec`）**：`run.content` 必须为 `"."` 或 `".<LocalPath>"`（禁止跨包绝对路径，禁止 `..` 上级相对路径）。
   * **匿名入口工作流（`__main__`）隔离约束**：当工作流为未挂载在任何具名 Python 包下的匿名入口文档（$\text{workflow\_id} = \texttt{"\_\_main\_\_"}$）时，因不存在对应的宿主物理 Python 包上下文，编译期静态禁止使用 `executor: "module"`（报 `module.anonymous_package` 错误）。
   * **闭合执行体**：`RunSpec::Module {content}` 只允许 `executor` 和 `content` 两个键；额外字段产生 `schema.unknown_field`，非法局部路径在 `/run/content` 产生 `schema.run`。匿名入口中任意嵌套层级均禁止 `module`。
3. **编译请求与静态包加载**：
   * 编译请求为闭合对象 `{entry, mounts}`。`mounts` 按公开顶层根或 `__main__` 组织，每项为闭合 `MountUnit {packages, resources, allowed_executors}`。两类文档均为点分包名到 `SourceDocument {format, text}` 的有序映射，默认空；执行器白名单默认为 `null`。重复键在解析时拒绝，文档只能归属自己的根。匿名根仅允许 `packages["__main__"]`，不能声明资源。
   * 宿主以显式根挂载构建快照，拒绝重复根、重叠物理树和同名包定义。编译器先检查所有资源声明，再解析入口工作流依赖闭包，先收集顶层签名，再检查实现；循环引用不依赖声明顺序，且不触发 Python 导入。每个文档使用所属根的执行器白名单，所有根复用同一编译管线。
   * 规范化原子根和图内 `run` 节点保存所属 `workflow_id`，内联图与迭代不会改变归属。解释器将该字段传给 Activity，业务模块只在 Activity 内加载。包名与逐层私有可见性详见 [文档文法](../01-grammar-and-ast/01-document-and-node-grammar.md) 和 [跨包调用规则](../03-static-flow-analysis/01-cfg-topology-and-sese-validation.md)。

---

## 二、 `__axcutor__.yaml` 工作流包同构目录结构与路径映射规则

设当前工作流包在挂载命名空间中的逻辑点分包路径为 $\text{workflow\_pkg}$（例如 `"order_flow"` 或 `"billing.order_flow"`），对应宿主源码根目录下的物理文件夹路径为 `<workflow_dir>/`。`run.content` 的合法形态与同级契约模板文件、业务实现文件及运行期 `importlib.import_module` 目标模块名的映射关系如下表所示：

下表采用逻辑根与 Python 根同名的默认映射。Worker 也可固定映射 `billing` 到 `company.billing`，此时 `billing.charge + .verify` 加载 `company.billing.charge.verify`。显式映射为封闭表，不能由提交请求注入或修改；缺少根映射产生 `execution` 错误。

| `run.content`（`LocalModuleSpec`） | 相对契约模板文件路径（相对于 `<workflow_dir>/`） | 相对业务实现文件路径（相对于 `<workflow_dir>/`） | 解析后的目标 Python 模块名 |
| :--- | :--- | :--- | :--- |
| `"."` | `_contract.py` | `__init__.py` | `<workflow_pkg>`（如 `"double"` 或 `"billing._risk_score"`） |
| `".<name>"`（如 `".charge_card"`） | `<name>_contract.py`（如 `charge_card_contract.py`） | `<name>.py`（如 `charge_card.py`） | `<workflow_pkg>.<name>`（如 `"order_flow.charge_card"`） |
| `".<sub_pkg>.<name>"` | `<sub_pkg>/<name>_contract.py` | `<sub_pkg>/<name>.py` | `<workflow_pkg>.<sub_pkg>.<name>` |

### 两类标准工作流包目录布局

1. **包含多个 `module` 节点的复合图工作流包（如 `order_flow`）**：
   ```txt
   order_flow/                             # 既是 Axcutor 工作流包，也是 Python Package order_flow
   ├── __init__.py                         # Python 包入口（可存放本包内节点共用的辅助函数）
   ├── __axcutor__.yaml                    # 工作流图定义（内部节点引用 .validate_order、.charge_card）
   ├── validate_order_contract.py          # [自动生成] 节点 validate_order 的契约模板与 SHA
   ├── validate_order.py                   # [人工编写] 节点 validate_order 的业务实现
   ├── charge_card_contract.py             # [自动生成] 节点 charge_card 的契约模板与 SHA
   └── charge_card.py                      # [人工编写] 节点 charge_card 的业务实现
   ```
2. **顶层单步 `run` 的原子工作流包（如 `double`，声明 `content: "."`）**：
   ```txt
   double/                                 # 既是原子 Axcutor 工作流包，也是 Python Package double
   ├── __axcutor__.yaml                    # 顶层 run: { executor: "module", content: "." }
   ├── _contract.py                        # [自动生成] 顶层 run 的契约模板与 SHA
   └── __init__.py                         # [人工编写] from ._contract import ... 并实现 async def execute
   ```

---

## 三、 确定性契约 SHA-256 指纹与契约模板（Contract Stub）生成规范

### 1. 节点契约 SHA-256 指纹计算（`compute_contract_sha256`）

给定编译器前端归一化导出的节点契约三元组 `(input_schema, output_schema, branches_contract)`，定义其规范化契约载荷为固定键序的 JSON 映射：

```json
{
  "input": <input_schema>,
  "output": <output_schema>,
  "branches": <branches_contract>
}
```

使用无损确定性 JSON 序列化器（`axcutor_runtime.codec.dumps`，紧凑无空白、严格保持各级映射字段声明顺序、无损保留十进制数值字面量）将上述结构编码为 UTF-8 字节序列，计算其 SHA-256 摘要并格式化为 64 位小写十六进制字符串，记为 `contract_sha256`。

### 2. 契约模板模块生成（`synthesize_contract_module`）

契约模板生成器完全复用 [`02-builtin-python-executor.md`](./02-builtin-python-executor.md) 第二、三节定义的 `TypeSpec` 到 Pydantic v2 强类型映射规则（包括只读冻结 `Input` / `Input_<path>`、严格校验 `Output` / `Output_<path>`、按分支裁剪输出子集的 `Branch.<branch_id>` 变体模型、非对称 `float` 数值边界转换以及可选字段缺省哨兵 `_UNSET`），生成不包含 `async def execute` 函数体的纯契约 `.py` 模板模块，并满足以下附加规约：

1. **模块顶层单一 SHA-256 常量**：
   在契约模板模块顶层声明单一字符串常量（不在 `Input`、`Output` 或 `Branch` 类上重复挂载哈希属性）：
   ```python
   __axcutor_contract_sha256__: str = "<64-lowercase-hex-chars>"
   ```
2. **显式导出列表 `__all__`**：
   * 无分支节点（`branches_contract == {}`）：`__all__ = ["Input", "Output", "ReturnType", ...]`（包含所有生成的嵌套结构体模型类名 `Input_<path>` 与 `Output_<path>`）；
   * 多分支节点（`branches_contract != {}`）：`__all__ = ["Input", "Branch", "ReturnType", ...]`（包含所有生成的嵌套结构体模型类名 `Input_<path>` 与 `Output_<path>`）。
3. **文件规划与检查入口**：
   `axcutor contracts` 依据挂载的物理目录生成同级模板；`--check` 只检查缺失或过期模板。两种模式均在写入前完成全部规划，拒绝模板覆盖实现文件、与已有 Python 包目录同名，以及同一实际实现文件承担不同契约。`.` 与 `.__init__` 指向同一实现文件，也须满足契约一致性。只有生成模式在全部检查通过后写入；生成过程不导入业务模块。

---

## 四、 业务实现模块编写契约与 `Input.__module__` 反查机制

### 1. 业务实现模块接口约定

业务模块（如 `<name>.py` 或当 `content == "."` 时的 `__init__.py`）从同级契约模板导入契约类型，并实现顶层异步入口函数 `async def execute(input: Input) -> ReturnType:`：

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

* **零冗余导入（Linter 友好）**：业务模块仅需导入实际使用的 `Input`、`Output`（或 `Branch`）、`ReturnType` 及嵌套模型类，无需导入 `__axcutor_contract_sha256__`，避免触发 `F401 unused-import` 告警。

### 2. 通过 `Input.__module__` 反查契约模板 SHA

在运行期加载目标业务模块 `mod` 后，`module` 执行器通过以下反射协议定位其绑定的契约模板模块并校验 SHA：

实现的 `Input` 必须是模板导出的原始 `Input` 类，`Output` 或 `Branch` 必须为同一模板导出的对象；仅有相同 SHA 不足以允许混用模板。所有输出变体必须属于该模板的严格模型。

1. 读取业务模块顶层属性 `input_cls = getattr(mod, "Input", None)`，校验 `input_cls` 为合法的契约模型类；
2. 核对输出模型同源性：
   * 若当前节点为无分支节点（`branches_contract == {}`）：读取 `output_cls = getattr(mod, "Output", None)`，校验其存在且 `output_cls.__module__ == input_cls.__module__`；
   * 若当前节点为多分支节点（`branches_contract != {}`）：读取 `branch_ns = getattr(mod, "Branch", None)`，校验其存在且类的模块来源与 `input_cls.__module__` 一致；读取来源时绕过分支命名空间属性钩子，以支持合法分支名 `__module__`；
3. 从 `sys.modules.get(input_cls.__module__)` 取得已加载的契约模板模块对象 `contract_mod`，读取其顶层常量 `actual_sha = getattr(contract_mod, "__axcutor_contract_sha256__", None)`。

---

## 五、 运行期执行协议与异常分类映射

当运行时激活 `run: { executor: "module", content: <LocalModuleSpec> }` 的单次尝试时，请求载荷除包含 [`01-run-executor-contract.md`](./01-run-executor-contract.md) 定义的标准字段外，还携带当前节点所属的工作流包路径 `workflow_id`（即 $\text{workflow\_pkg}$）。`module` 执行器按以下顺序执行并映射结果：

1. **包内相对路径解析与模块导入**：
   * 先按 Worker 固定根映射将逻辑 `workflow_pkg` 转为 Python 包名 `python_pkg`，再结合单点相对路径 `content` 解析目标模块：
     * 当 `content == "."` 时，目标模块名为 `python_pkg`；
     * 当 `content == ".<rel_path>"` 时，目标模块名为 `f"{python_pkg}{content}"`；
     * 导入前检查各路径组成部分的包／文件布局；`.` 的目标为包，局部实现的末段为文件模块，同名 Python 包目录与 `.py` 文件并存时拒绝加载；
   * 若模块导入期间抛出 `ModuleNotFoundError`、`ImportError`、`SyntaxError` 或其他顶层初始化异常，捕获并映射为标准可恢复异常 **`execution`**；
   * 检查 `getattr(mod, "execute", None)` 是否存在且满足 `inspect.iscoroutinefunction(mod.execute)`；若缺失 `execute` 或 `execute` 不是异步协程函数，立即抛出 `ContractError`，映射为标准可恢复异常 **`contract`**。
2. **契约模板 SHA 一致性校验（Schema Drift Check）**：
   * 根据当前请求的 `(input_schema, output_schema, branches_contract)` 计算期望指纹 `expected_sha = compute_contract_sha256(input_schema, output_schema, branches_contract)`；
   * 按第四节反查机制通过 `mod.Input.__module__` 定位 `contract_mod`（同时验证 `mod.Output` 或 `mod.Branch` 与 `mod.Input` 归属于同一契约模块）并提取 `actual_sha = getattr(contract_mod, "__axcutor_contract_sha256__", None)`；
   * 若 `mod` 未导入合法的契约模型类，或 `mod.Output` / `mod.Branch` 与 `mod.Input` 来源不一致，立即抛出说明契约来源错误的 `ContractError`；若无法读取 SHA 或 `actual_sha != expected_sha`，抛出包含预期与实际 SHA 的 `ContractError`。两类错误均映射为标准可恢复异常 **`contract`**。
3. **只读输入快照构造与协程求值**：
   * 调用 `contract_mod.Input.model_validate(deepcopy(input_snapshot))` 构造冻结的只读 `input_model`（自动将传输层 `Decimal` 转换为原生 `float`）；若输入边界转换失败，抛出 `ContractError`（映射为 **`contract`** 异常）；
   * 在事件循环中启动 `asyncio.create_task(mod.execute(input_model))` 并监听 `abort_signal`；一旦取消信号触发，立即向任务注入 `asyncio.CancelledError` 并等待其排空退出。
4. **返回值校验、导出与异常归类**：
   * **正常返回合规实例**：校验返回值 `result` 必须为该契约模块 `contract_mod` 定义的 `Output` 实例（无分支节点）或 `Branch.<branch_id>` 变体实例（多分支节点）；校验通过后提取 `branch = type(result)._branch` 与剥离 `contract_mod._UNSET` 后的字典 `output`，返回给外层运行时执行最终的 `validate_run_contract` 边界核验与原子写回；
   * **契约违约映射为 `contract` 异常**：
     * 返回值不属于 `contract_mod` 定义的对应 `Output` / `Branch.<branch_id>` 类型；
     * 或业务代码在构造 `Output(...)`、`Branch.<branch_id>(...)`、`Output_<path>(...)` 期间触发了归属于 `contract_mod._StrictModel` 子类的 `pydantic.ValidationError`；
     上述情形统一捕获并映射为标准可恢复异常 **`contract`**；
   * **业务与运行时故障映射为 `execution` 异常**：
     * `mod.execute(input_model)` 执行期间抛出的除上述契约违约与 `CancelledError` 以外的任何未捕获异常，直接保留 Python 物理 `.py` 文件路径与真实源码行号堆栈，映射为标准可恢复异常 **`execution`**。

输入与嵌套模型冻结，列表与映射保留 Python 容器语义；修改这些容器不会影响原输入快照。使用 Pydantic 构造旁路产生的非法输出仍由 Rust 边界拒绝；无关业务模型的 Pydantic 校验异常保持 `execution` 分类。

`builtin` 与 `module` 共用单次 Python 调用生命周期：构造隔离输入、竞速业务任务与取消信号、核验并导出输出，所有退出路径均取消并排空所拥有任务。源码合成或模块加载、契约来源检查与各自的错误位置诊断保留在执行器入口。外层 Activity 管理心跳、deadline 与最终 Rust 校验，超时和取消优先于迟到返回值。Python 导入缓存属于宿主进程；工作流快照只固定 DSL，不固定或携带 Python 实现版本。
