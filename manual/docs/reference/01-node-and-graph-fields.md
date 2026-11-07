# 01. 节点与图字段速查表

本页汇总 Axcutor YAML 工作流文件中各层级的配置字段、类型格式与默认值，并提供各层级结构的对照示例片段。

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
| `nodes` | **必填** | `{ <node_id>: <NodeSpec> }` | 图内非空子节点字典（节点名不可使用保留字 `start`、`end`、`fail`）。 |
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
| **执行体鉴别字段** | `run`（**必填**） | `graph`（**必填**） | `use: workflow:<path> \| builtin:<op>`（**必填**） |
| `input` | 可选，`[<var_name>, ...]`（默认 `[]`） | 可选，`[<var_name>, ...]`（默认 `[]`） | *不支持*（通过 `with.input` 绑定） |
| `output` | 可选，`{ <var>: <TypeSpec> }`（默认 `{}`） | 可选，`{ <var>: <TypeSpec> }`（默认 `{}`） | *不支持*（通过 `with.output` 映射） |
| `with` | *不支持* | *不支持* | 可选，`{ input?: {...}, output?: {...} }` |
| `branches` | 可选 | 可选 | *自动继承自被调用目标* |
| `for_each` | 可选 | 可选 | 可选（仅限 `workflow:<path>`，`builtin:*` 禁用） |
| `timeout_ms` | 可选 | 可选 | 可选（仅限 `workflow:<path>`，`builtin:*` 禁用） |
| `retry` | 可选 | 可选 | 可选（仅限 `workflow:<path>`，`builtin:*` 禁用） |
| `catch` | 可选 | 可选 | 可选（仅限 `workflow:<path>`，`builtin:*` 禁用） |

- **挂载 `for_each` 时的输入与输出特殊规则**：
  - **`input` 注入项差异**：`run` 节点须在 `input` 中显式列出其读取的 `item` / `key` / `index` 注入项及外层变量；内联 `graph` 子图节点已将 `item` / `key` / `index` 自动注入子图作用域，其 `input` 仅用于捕获外层变量，**禁止**重复列出 `for_each` 注入项。
  - **`output` / `with.output` 非空与集合类型提升**：挂载 `for_each` 时，`run` 与内联 `graph` 节点的 `output`（声明单次迭代元素类型 `U`）以及 `use` 节点的 `with.output` 均变为**必填且非空**，并在外层符号表中一阶同构提升为集合类型（`Array<U>` 或 `OrderedMap<string, U>`）。
- **`use: workflow:<path>` 与 `use: builtin:<op>` 的修饰符差异**：
  - `for_each`、`timeout_ms`、`retry`、`catch` 四类修饰符仅支持 `use: workflow:<path>` 子工作流调用，对所有 `use: builtin:<op>` 内建算子禁用。

---

## 四、 节点修饰符子结构字段表

### 1. `with`（用于 `use` 节点）

| 字段 | 格式 | 说明 |
| :--- | :--- | :--- |
| `with.input.<param>` | `var_name` \| `root_var.field` \| `123` \| `true` \| `null` \| `{ value: <literal> }` | 将当前作用域变量、非空结构体字段路径或常量字面量绑定至目标 `input` 参数。所有必填目标参数均须显式声明，不支持隐式同名省略（同名时须写 `<param>: <param>`）。 |
| `with.output.<out_field>` | `<target_var_name>` | 将目标产出的 `output` 字段映射写回当前图符号表中已声明的顶层变量 `<target_var_name>`（禁止将值设为 `null`）。 |

- **字段投影与参数绑定规则（`with.input`）**：
  - 传入结构体字段路径 `root_var.field` 时，若最末级字段未列入宿主 `object` 的 `required`，其投影类型会自动提升为可空联合类型 `[<BaseType>, "null"]`。
  - 无论 `use: workflow:<path>` 还是 `use: builtin:<op>`，目标的所有必填输入参数均须在 `with.input` 中显式声明。
- **`use: workflow:<path>` 与 `use: builtin:<op>` 的输出映射差异（`with.output`）**：
  - **`use: workflow:<path>`**：支持按需子集映射；未在 `with.output` 中列出的目标输出字段默认自动忽略（不要求在调用方符号表中存在、不写回调用方栈帧）。
  - **`use: builtin:<op>`**：
    - `builtin:switch`：**禁止**声明 `with.output`。
    - `builtin:compare` 与 `builtin:logic`：`with.output` **可选**；省略时直接暴露 `.true` / `.false` 控制分支，显式声明 `result` 映射时为无分支单出边。
    - 其余内建算子：`with.output` **必填**。
- **挂载 `for_each` 时的输出映射规则**：
  - 当节点挂载 `for_each` 时，`with.output` 必填非空，且映射的外层目标变量 `<target_var_name>` 类型对应为一阶同构提升后的集合类型（`Array<U>` 或 `OrderedMap<string, U>`）。

### 2. `for_each`（用于集合遍历）

| 字段 | 必填/可选 | 格式 | 说明 |
| :--- | :--- | :--- | :--- |
| `source` | **必填** | `var_name` \| `root_var.field` | 绑定已定值的非空强类型列表（`array`）或有序映射（`OrderedMap`）。 |
| `item` | **必填** | `<identifier>` | 注入单次迭代帧的当前元素（或有序映射的当前值）变量名。 |
| `key` | 可选 | `<identifier>` | 仅遍历有序映射（`OrderedMap`）时可用：注入当前键（`string`）的变量名；遍历列表时禁止声明。 |
| `index` | 可选 | `<identifier>` | 注入当前遍历序号 `0, 1, ...`（`integer`）的变量名。 |
| `concurrency` | 可选 | 正整数（默认 `1`） | 最大并发度（正整数，默认 `1`）。 |

*(注：挂载 `for_each` 的节点直接复用节点自身的 `output`、`with.output` 与 `branches.<branch>.output` 声明目标输出集合变量与按分支过滤子集，要求至少绑定 1 个目标输出变量。)*

### 3. `retry` 与 `catch`（用于容错恢复）

| 字段 | 格式 | 说明 |
| :--- | :--- | :--- |
| `retry.max_attempts` | 整数 `>= 2`（必填） | 最大尝试总次数（含首次执行）。 |
| `retry.backoff_ms` | 非负整数（默认 `0`） | 两次尝试之间的退避等待毫秒数。 |
| `retry.on` | `[execution, timeout, contract, max_steps]` 子集 | 触发重试的可恢复异常列表（默认 `[execution, timeout]`；`run` 节点与顶层 `run` 根节点禁止包含 `max_steps`）。 |
| `catch.<branch_name>` | `[<selector>, ...]` \| `"*"` | 拦截指定异常（或使用 `"*"` 拦截其余所有可恢复异常），物化为异常出边 `<node>.<branch_name>`（等价于 `output: []`；`run` 节点的显式选择器列表中禁止包含 `max_steps`）。 |
