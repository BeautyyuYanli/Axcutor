# 01. 节点与图字段速查表

本页汇总 Axcutor YAML 工作流文件中各层级的配置字段、类型格式与默认值，并提供各层级结构的对照示例片段。

---

## 一、 文件顶层：根节点（Root Node）字段表

| 字段 | 必填/可选 | 类型 / 格式 | 说明 |
| :--- | :--- | :--- | :--- |
| `input` | **必填** | `{ <var_name>: <TypeSpec> }` | 工作流对外输入参数表；无输入时写 `{}`。作为宿主根入口启动（或位于 `"__main__"`）时，其中出现的 `handle` 须在 `__ax_resources__.yaml` 中声明公开 `locator`。 |
| `output` | **必填** | `{ <var_name>: <TypeSpec> }` | 工作流对外输出参数表；无输出时写 `{}`。作为宿主根入口启动（或位于 `"__main__"`）时，其中出现的 `handle` 须声明公开 `locator`（具名包仅供内部 `use: workflow:...` 调用时可传递内部句柄）。 |
| `branches` | 可选 | `{ <choice>: "<desc>" \| { description: "<desc>", output: [<var>, ...] } }` | 对外控制分支及各分支承诺产出的 `output` 字段子集，默认 `{}`。 |
| `timeout_ms` | 可选 | 正整数（毫秒） | 仅当顶层为 `run` 时可用：单次尝试最大允许耗时。Activity 节点省略时采用运行选项 `activity_timeout_ms`（默认 7 天）；`signal` 节点省略时无限期等待。 |
| `retry` | 可选 | `RetrySpec` | 仅当顶层为 `run` 时可用：原地快照重试配置。 |
| `run` \| `graph` | **二选一** | `RunSpec` 或 `GraphSpec` | 根节点执行体：直接作为单步动作（`run`）或展开为控制流图（`graph`）。 |

```yaml
# 根节点字段示例（顶层为 graph）
input:
  request_id: { type: string }
output:
  report: { type: string }
branches:
  ok:
    description: "处理完成"
    output: [report]
  skipped:
    description: "无需处理"
    output: []
graph: ...
```

---

## 二、 控制流图容器（`graph`）字段表

| 字段 | 必填/可选 | 类型 / 格式 | 说明 |
| :--- | :--- | :--- | :--- |
| `variables` | 可选 | `{ <var>: { <TypeSpec>, default?: <literal> } }` | 图级局部变量声明表（不可与当前图的宿主输入变量重名）。每个值必须是一个良构的 `TypeSpec` 映射对象，可选配 `default: <literal>`：包含 `default` 时在图启动处赋予初始定值；省略 `default` 时仅在符号表中声明变量类型，图启动时处于未定值状态（`Unbound`）。 |
| `max_steps` | 可选 | 正整数（默认 `1000`） | 当前图栈帧允许调度的最大步数上限（用于包含环路的图）。 |
| `nodes` | **必填** | `{ <node_id>: <NodeSpec> }` | 图内非空子节点字典（节点名及所有变量名、分支名、结构体字段名均不可使用控制流保留字 `start`、`end`、`fail` 或 Python 硬关键字）。 |
| `edges` | **必填** | `[ "<edge_expr>", ... ]` | 非空控制流连边列表（详见 [02. 6 种控制边语法速查表](./02-edge-syntax-cheatsheet.md)）。 |

```yaml
graph:
  max_steps: 50
  variables:
    attempt_count:
      type: integer
      default: 0
    logs:
      type: array
      items: { type: string }
      default: []
    current_item:
      type: string
  nodes: ...
  edges: ...
```

---

## 三、 图内三类节点（`graph.nodes.<id>`）字段对照表

| 字段 | `run` 动作节点 | 内联 `graph` 子图节点 | `use` 调用节点 |
| :--- | :--- | :--- | :--- |
| **执行体鉴别字段** | `run: { executor: ..., ... }`（**必填**） | `graph`（**必填**） | `use: workflow:<PackagePath> \| builtin:<op>`（**必填**） |
| `input` | 可选，`[<var_name>, ...]`（默认 `[]`） | 可选，`[<var_name>, ...]`（默认 `[]`） | *不支持*（通过 `with.input` 绑定） |
| `output` | 可选，`{ <var>: <TypeSpec> }`（默认 `{}`） | 可选，`{ <var>: <TypeSpec> }`（默认 `{}`） | *不支持*（通过 `with.output` 映射） |
| `with` | *不支持* | *不支持* | 可选，`{ input?: {...}, output?: {...} }` |
| `branches` | 可选 | 可选 | *自动继承自被调用目标* |
| `for_each` | 可选 | 可选 | 可选（仅限 `workflow:<PackagePath>`，`builtin:*` 禁用） |
| `timeout_ms` | 可选 | 可选 | 可选（仅限 `workflow:<PackagePath>`，`builtin:*` 禁用） |
| `retry` | 可选 | 可选 | 可选（仅限 `workflow:<PackagePath>`，`builtin:*` 禁用） |
| `catch` | 可选 | 可选 | 可选（仅限 `workflow:<PackagePath>`，`builtin:*` 禁用） |

- **挂载 `for_each` 时的输入与输出特殊规则**：
  - **`input` 注入项差异**：`run` 节点须在 `input` 中显式列出其读取的 `item` / `key` / `index` 注入项及外层变量；内联 `graph` 子图节点已将 `item` / `key` / `index` 自动注入子图作用域，其 `input` 仅用于捕获外层变量，**禁止**重复列出 `for_each` 注入项。
  - **`output` / `with.output` 非空与集合类型提升**：挂载 `for_each` 时，`run` 与内联 `graph` 节点的 `output`（声明单次迭代元素类型 `U`）以及 `use` 节点的 `with.output` 均变为**必填且非空**，并在外层符号表中一阶同构提升为集合类型（`Array<U>` 或 `OrderedMap<string, U>`）。
- **`use: workflow:<PackagePath>` 与 `use: builtin:<op>` 的修饰符及可见性规则**：
  - `for_each`、`timeout_ms`、`retry`、`catch` 四类修饰符仅支持 `use: workflow:<PackagePath>` 子工作流包调用，对所有 `use: builtin:<op>` 内建算子禁用。
  - `workflow:<PackagePath>` 必须为点号 `.` 分隔的合法包路径（首段为公开标识符，后续段若以 `_` 开头则为父包私有子包，仅允许从父包子树内调用）。
- **`timeout_ms` 省略时的默认期限**：
  - Activity 型 `run`（`builtin`、`module` 及扩展执行器）省略 `timeout_ms` 时，单次尝试采用运行选项 `activity_timeout_ms`（默认 `604800000` 毫秒，即 7 天；排队时间不计入尝试预算）；
  - 内联 `graph`、`use: workflow:<PackagePath>` 与 `executor: signal` 省略 `timeout_ms` 时不设单次尝试上限（详见 [运行选项、心跳与宿主资源](../guide/04-state-and-resilience/02-guardrails-and-timeouts.md#runtime-options)）。

---

## 四、 `run` 执行体配置与节点修饰符子结构字段表

### 1. `run`（结构化业务执行体配置）

`run` 字段必须是一个以 `executor` 为鉴别键的映射对象（不支持裸字符串简写）。两类内建 Python 执行器（`builtin` 与 `module`）仅包含 `executor` 和 `content`；协作式信号等待执行器 `signal` 仅包含 `executor`，三者均为闭合对象：

| 字段 | 必填/可选 | 格式 | 说明 |
| :--- | :--- | :--- | :--- |
| `executor` | **必填** | `Identifier` | 执行器类型鉴别键：`builtin`、`module` 与 `signal` 使用闭合配置，其他合法标识符对应宿主注册的扩展执行器，其特定配置字段保留并透传给对应后端。此处使用通用标识符，不排除 Python 关键字。 |
| `content`（当 `executor: builtin` 时） | **必填** | 非空多行 Python 代码字符串 | 作为 `async def execute(input: Input) -> ReturnType:` 的函数体执行。通过只读冻结模型 `input.<var>` 读取输入快照（其中 `type: number` 自动映射为原生 `float`）；无分支节点返回 `Output(...)`，有分支节点返回 `Branch.<branch_id>(...)`。 |
| `content`（当 `executor: module` 时） | **必填** | `LocalModuleSpec`（`"."` 或 `".<local_path>"`） | 指向当前具名工作流包内的 Python 实现；匿名入口 `"__main__"` 禁用，禁止绝对模块名与上级跳转。顶层 `run` 与图内节点均支持 `".validate"`（对应 `validate_contract.py`）或 `"."`（对应包的 `__init__.py` 与 `_contract.py`）。目标模块须从同一伴生契约桩导入 `Input`、`Output` 或 `Branch`，并实现异步 `execute`；`ReturnType` 可用于返回值注解。 |
| *无附加字段*（当 `executor: signal` 时） | — | 闭合映射 `{ executor: signal }` | 在工作流协程内挂起等待外部信号，不派发 Activity 任务或心跳，也不生成 Python 契约桩（附加 `content` 等未知字段触发编译错误 `schema.unknown_field`）。详见 [04. signal 协作式等待执行器规范](../../spec/05-executor-abi/04-signal-cooperative-executor.md)。 |

- **`builtin` 与 `module` 执行器补充约定**：
  - **嵌套结构体显式构造**：结构体输出必须显式构造生成的 `Output_<path>` 模型。非必填属性可以省略；只有属性类型可空时才允许显式传入 `None`：
    ```python
    # 假设 user.nickname 非必填且不可空，user.note 非必填且可空。
    return Output(user=Output_user(id=1, note=None))  # 输出省略 nickname，保留 note: null。
    ```
  - **Worker 路由与源码合成**：Python Worker 默认同时提供 `builtin` 与 `module` 执行器；`options.task_queues` 按执行器名路由。需要导出 `builtin` 节点独立 Python 源码进行类型检查时，可调用 `axcutor_runtime.builtin.synthesize_module(input_schema, output_schema, branches_contract, content)`（返回 `source` 与 `line_offset`）；对于 `module` 节点，`axcutor contracts`（或 `synthesize_contract_module`）生成含 `__axcutor_contract_sha256__` 指纹的契约桩，运行时强制比对指纹（见 [04. 包挂载与 Python 模块](../guide/01-foundations/04-packages-and-modules.md)）。
  - **生成模型边界**：生成源码统一使用 Python 3.11 支持的 `TypeAlias` 表达返回联合类型；数值枚举由严格数值注解与枚举校验器共同约束（因 Python `Literal` 不支持小数字面量）。超过原生有限 `float` 范围的 `number` 输入在模型边界产生 `contract` 异常。
- **`signal` 协作式等待执行器补充约定**：
  - **信号投递与契约校验**：通过 [HTTP `:signal`](./04-execution-http-api.md#投递外部信号) 或 CLI `axcutor signal` 按 `Frame.waiting` 中的规范地址提交符合节点 `output` / `branches` 契约的响应。节点的 `input` 声明参与编译期必达定值分析（可引用包括内部私有 `handle` 在内的任意作用域变量），而 `output` 中若包含 `type: handle`，对应资源须声明公开 `locator`（违反时报 `type.signal_handle`，外部按 `{"$handle": "<kind>", "locator": ...}` 提交）。
  - **超时、异常域与并发屏障**：省略 `timeout_ms` 时无限期等待（不套用 Activity 默认期限）。非法外部信号在投递边界被拒绝（`422`），节点保持等待且不消耗重试预算；`retry.on` 默认且仅允许 `[timeout]`，`catch` 的 `"*"` 仅对 `{timeout}` 求补，声明其他选择器将触发编译错误。在 Fork-Join 与 `for_each` 中，单分支或单元素的信号输出仍在汇合屏障处统一提交，迭代等待期间持续持有 `concurrency` 并发许可：
    ```yaml
    decision:
      output:
        reviewer: { type: string }
      timeout_ms: 86400000
      catch:
        expired: [timeout]
      run:
        executor: signal
    ```

### 2. `with`（用于 `use` 节点）

| 字段 | 格式 | 说明 |
| :--- | :--- | :--- |
| `with.input.<param>` | `var_name` \| `root_var.field` \| `123` \| `true` \| `null` \| `{ value: <literal> }` | 将当前作用域变量、非空结构体字段路径或常量字面量绑定至目标 `input` 参数。所有必填目标参数均须显式声明，不支持隐式同名省略（同名时须写 `<param>: <param>`）。 |
| `with.output.<out_field>` | `<target_var_name>` | 将目标产出的 `output` 字段映射写回当前图符号表中已声明的顶层变量 `<target_var_name>`（禁止将值设为 `null`）。 |

- **字段投影与参数绑定规则（`with.input`）**：
  - 传入结构体字段路径 `root_var.field` 时，若最末级字段未列入宿主 `object` 的 `required`，其投影类型会自动提升为可空联合类型 `[<BaseType>, "null"]`。
  - 无论 `use: workflow:<PackagePath>` 还是 `use: builtin:<op>`，目标的所有必填输入参数均须在 `with.input` 中显式声明。
- **`use: workflow:<PackagePath>` 与 `use: builtin:<op>` 的输出映射差异（`with.output`）**：
  - **`use: workflow:<PackagePath>`**：支持按需子集映射；未在 `with.output` 中列出的目标输出字段默认自动忽略（不要求在调用方符号表中存在、不写回调用方栈帧）。
  - **`use: builtin:<op>`**：
    - `builtin:switch`：**禁止**声明 `with.output`。
    - `builtin:compare` 与 `builtin:logic`：`with.output` **可选**；省略时直接暴露 `.true` / `.false` 控制分支，显式声明 `result` 映射时为无分支单出边。
    - 其余内建算子：`with.output` **必填**。
- **挂载 `for_each` 时的输出映射规则**：
  - 当节点挂载 `for_each` 时，`with.output` 必填非空，且映射的外层目标变量 `<target_var_name>` 类型对应为一阶同构提升后的集合类型（`Array<U>` 或 `OrderedMap<string, U>`）。

### 3. `for_each`（用于集合遍历）

| 字段 | 必填/可选 | 格式 | 说明 |
| :--- | :--- | :--- | :--- |
| `source` | **必填** | `var_name` \| `root_var.field` | 绑定已定值的非空强类型列表（`array`）或有序映射（`OrderedMap`）。 |
| `item` | **必填** | `<identifier>` | 注入单次迭代帧的当前元素（或有序映射的当前值）变量名。 |
| `key` | 可选 | `<identifier>` | 仅遍历有序映射（`OrderedMap`）时可用：注入当前键（`string`）的变量名；遍历列表时禁止声明。 |
| `index` | 可选 | `<identifier>` | 注入当前遍历序号 `0, 1, ...`（`integer`）的变量名。 |
| `concurrency` | 可选 | 正整数（默认 `1`） | 同一集合激活内同时执行的最大迭代许可数（正整数，默认 `1`；迭代的挂起等待、内部重试与取消清理期间持续持有许可，不等同于 Worker 全局进程槽位）。 |

*(注：挂载 `for_each` 的节点直接复用节点自身的 `output`、`with.output` 与 `branches.<branch>.output` 声明目标输出集合变量与按分支过滤子集，要求至少绑定 1 个目标输出变量。)*

### 4. `retry` 与 `catch`（用于容错恢复）

| 字段 | 格式 | 说明 |
| :--- | :--- | :--- |
| `retry.max_attempts` | 整数 `>= 2`（必填） | 最大尝试总次数（含首次执行）。 |
| `retry.backoff_ms` | 非负整数（默认 `0`） | 两次尝试之间的退避等待毫秒数。 |
| `retry.on` | `[execution, timeout, contract, max_steps]` 子集 | 普通节点默认 `[execution, timeout]`；`run` 禁止 `max_steps`；`signal` 仅允许 `[timeout]`，也以此为默认值。 |
| `catch.<branch_name>` | `[<selector>, ...]` \| `"*"` | 拦截指定异常，`"*"` 对当前节点的允许集合求补；异常出边无输出。`run` 禁止 `max_steps`；`signal` 的允许集合为 `{timeout}`。 |
