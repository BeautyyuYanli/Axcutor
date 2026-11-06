# 01. 节点与图字段速查表

本页汇总 Axcutor YAML 工作流文件中各层级的合法配置字段、类型与默认值。

---

## 一、 文件顶层：根节点（Root Node）字段表

| 字段 | 必填/可选 | 类型 / 格式 | 说明 |
| :--- | :--- | :--- | :--- |
| `input` | **必填** | `{ <var_name>: <TypeSpec> }` | 工作流对外输入参数表；无输入时写 `{}`。 |
| `output` | **必填** | `{ <var_name>: <TypeSpec> }` | 工作流对外输出参数表；无输出时写 `{}`。 |
| `branches` | 可选 | `{ <choice>: "<desc>" \| { description: "<desc>", output: [<var>, ...] } }` | 对外控制分支及各分支承诺产出的 `output` 字段子集，默认 `{}`。 |
| `timeout_ms` | 可选 | 正整数（毫秒） | 仅当顶层为 `run` 时可用：单次尝试最大允许耗时。 |
| `retry` | 可选 | `RetrySpec` | 仅当顶层为 `run` 时可用：原地快照重试配置。 |
| `run` \| `graph` | **二选一** | `RunSpec` 或 `GraphSpec` | 根节点执行体：直接作为单步动作（`run`）或展开为控制流图（`graph`）。 |

---

## 二、 控制流图容器（`graph`）字段表

| 字段 | 必填/可选 | 类型 / 格式 | 说明 |
| :--- | :--- | :--- | :--- |
| `variables` | 可选 | `{ <var>: <literal> \| { <TypeSpec>, default: <literal> } }` | 图级初始状态变量表（禁止与当前图的宿主输入变量重名）。 |
| `max_steps` | 可选 | 正整数（默认 `1000`） | 当前图栈帧允许调度的最大步数上限（用于包含环路的图）。 |
| `nodes` | **必填** | `{ <node_id>: <NodeSpec> }` | 图内子节点字典（节点名不可使用保留字 `start`、`end`、`fail`）。 |
| `edges` | **必填** | `[ "<edge_expr>", ... ]` | 控制流连边列表（详见 [02. 边语法速查表](./02-edge-syntax-cheatsheet.md)）。 |

---

## 三、 图内三种节点（`graph.nodes.<id>`）字段对照表

| 字段 | `run` 动作节点 | 内联 `graph` 子图节点 | `use` 调用节点 |
| :--- | :--- | :--- | :--- |
| **执行体鉴别字段** | `run`（**必填**） | `graph`（**必填**） | `use: workflow:<path> \| builtin:<op>`（**必填**） |
| `input` | 可选，`[<var_name>, ...]`（默认 `[]`） | 可选，`[<var_name>, ...]`（捕获外层变量，默认 `[]`） | *不支持*（通过 `with.input` 绑定） |
| `output` | 可选，`{ <var>: <TypeSpec> }`（默认 `{}`） | 可选，支持 **`[<var>, ...]` 列表简写** 或 `{ <var>: <TypeSpec> }` | *不支持*（通过 `with.output` 映射） |
| `with` | *不支持* | *不支持* | 可选，`{ input?: {...}, output?: {...} }` |
| `branches` | 可选 | 可选 | *自动继承自被调用目标* |
| `for_each` | 可选 | 可选 | 可选（仅限 `workflow:<path>`，`builtin:*` 禁止） |
| `timeout_ms` | 可选 | 可选 | 可选（仅限 `workflow:<path>`，`builtin:*` 禁止） |
| `retry` | 可选 | 可选 | 可选（仅限 `workflow:<path>`，`builtin:*` 禁止） |
| `catch` | 可选 | 可选 | 可选（仅限 `workflow:<path>`，`builtin:*` 禁止） |

---

## 四、 节点修饰符子结构字段表

### 1. `with`（用于 `use` 节点）

| 字段 | 格式 | 说明 |
| :--- | :--- | :--- |
| `with.input.<param>` | `var_name` \| `root_var.field` \| `123` \| `true` \| `null` \| `{ value: <literal> }` | 将当前作用域变量、非空结构体字段路径或常量字面量绑定到目标 `input` 参数。省略某项时默认按同名顶层变量匹配。 |
| `with.output.<out_field>` | `<target_var_name>` | 将目标产出的 `output` 字段映射写回当前图的顶层变量 `<target_var_name>`。省略某项时默认保持原字段名写回。 |

### 2. `for_each`（用于集合遍历）

| 字段 | 必填/可选 | 格式 | 说明 |
| :--- | :--- | :--- | :--- |
| `source` | **必填** | `var_name` \| `root_var.field` | 绑定已定值的非空强类型列表（`array`）或有序映射（`OrderedMap`）。 |
| `item` | **必填** | `<identifier>` | 注入单次迭代帧的当前元素（或有序映射的当前值）变量名。 |
| `key` | 可选 | `<identifier>` | 仅遍历有序映射时可用：注入当前键（`string`）的变量名。 |
| `index` | 可选 | `<identifier>` | 注入当前遍历序号 `0, 1, ...`（`integer`）的变量名。 |
| `concurrency` | 可选 | 正整数（默认 `1`） | 最大并发度；当声明了 `state` 时必须为 `1`。 |
| `filter` | 可选 | `{ <branch>: <target_var> }` | 按单次迭代所选分支（含 `catch` 分支），零拷贝将原元素保留至 `<target_var>`。 |
| `collect` | 可选 | `{ <field>: <target_var> \| { into: <target_var>, flatten: true } }` | 收集单次迭代产出的字段并提升为外层集合变量 `<target_var>`。 |
| `state` | 可选 | `[<state_var>, ...]` | 跨元素串行原地累积更新的外层已定值变量列表（强制 `concurrency: 1`）。 |

### 3. `retry` 与 `catch`（用于容错恢复）

| 字段 | 格式 | 说明 |
| :--- | :--- | :--- |
| `retry.max_attempts` | 整数 `>= 2`（必填） | 最大尝试总次数（含首次执行）。 |
| `retry.backoff_ms` | 非负整数（默认 `0`） | 两次尝试之间的退避等待毫秒数。 |
| `retry.on` | `[execution, timeout, contract, max_steps]` 子集 | 允许触发重试的可恢复异常列表（默认 `[execution, timeout]`；`run` 节点不可含 `max_steps`）。 |
| `catch.<branch_name>` | `[<selector>, ...]` \| `"*"` | 拦截指定异常（或用 `"*"` 拦截其余所有可恢复异常），物化为异常出边 `<node>.<branch_name>`（等价于 `output: []`）。 |
