# 02. 数据绑定、`with` 映射与静态字段路径文法规范

本规范定义受限变量标识符、静态字段路径投影表达式（`FieldPathExpr`），以及 `use` 节点中 `with` 映射的语法消歧、显式传参与显式输出接收契约。

---

## 一、 标识符与静态字段路径词法规则

1. **受限变量与字段标识符（`RestrictedIdentifier`）**：
   $$\text{Identifier} ::= \text{[a-zA-Z\_][a-zA-Z0-9\_]*}, \quad \text{RestrictedIdentifier} ::= \text{Identifier} \setminus \mathcal{R}$$
   禁止包含点号 `.`、空格或连字符 `-`，且不得属于 [`01-document-and-node-grammar.md`](./01-document-and-node-grammar.md) 定义的全局保留字集合 $\mathcal{R} = \mathcal{R}_{\text{cfg}} \cup \mathcal{R}_{\text{py}}$。
2. **静态字段路径表达式（`FieldPathExpr`）**：
   $$\text{FieldPathExpr} ::= v_0 (\text{"."} f_i)^* \quad (i \ge 0,\; v_0, f_i \in \text{RestrictedIdentifier})$$
   * 当 $k = 0$ 时，退化为顶层根变量引用 $v_0$；
   * 当 $k \ge 1$ 时，$E = v_0 . f_1 . f_2 \dots f_k$ 表示对根变量 $\text{RootVar}(E) = v_0$ 的 $k$ 级静态字段路径投影。

---

## 二、 允许出现 `FieldPathExpr` 与仅限顶层 `RestrictedIdentifier` 的语法位置

为确保图级变量表（`variables`）的写入与节点级快照捕获保持顶层原子性（无部分字段原地可变副作用），语法解析器严格区分只读投影位置与顶层捕获/写入位置：

| 语法位置 | 允许的表达式形态 | 规范约束 |
| :--- | :--- | :--- |
| **`for_each.source`** | `FieldPathExpr` ($k \ge 0$) | 支持绑定顶层变量名或非空 `TypedDict` 的嵌套字段路径。 |
| **`use.with.input.<param>`**（当值为字符串时） | `FieldPathExpr` ($k \ge 0$) | 支持绑定顶层变量名或非空 `TypedDict` 的嵌套字段路径。 |
| **`run.input` / `graph.input`** | 仅限顶层 `RestrictedIdentifier` ($k = 0$) | 禁止出现点号 `.`；`run` 节点按此白名单向执行器注入强类型输入快照（如 `builtin` / `module` 执行器的 `input.<var>`）。 |
| **`output` / `with.output.<field>`** | 仅限顶层 `RestrictedIdentifier` ($k = 0$) | 禁止出现点号 `.`。 |
| **`for_each.item` / `key` / `index`** | 仅限顶层 `RestrictedIdentifier` ($k = 0$) | 禁止出现点号 `.`。 |

---

## 三、 `use.with` 绑定语法、`{ value: <literal> }` 消歧与输出映射规则

在 `CallNode`（`use: workflow:<pkg_path>` 或 `use: builtin:<op>`）中，`with` 包含可选的 `input` 映射与 `output` 映射。

### 1. `with.input` 与 `with.output` 必填参数显式声明约束

无论 `CallNode` 是调用外部子工作流包（`use: workflow:<pkg_path>`）还是调用内建算子（`use: builtin:<op>`），**均不支持 `with.input` 的隐式同名省略**：

* **对 `use: workflow:<pkg_path>`**：必须满足 $\text{dom}(N.\text{with.input}) = \text{dom}(\text{target.input})$（当 $\text{target.input}$ 非空时，每个目标参数 $p \in \text{dom}(\text{target.input})$ 均须在 `with.input` 中显式写出；若与外层变量同名，写作 `p: p`；当 $\text{dom}(\text{target.input}) = \emptyset$ 时，允许省略 `with.input`）；缺失任何目标参数或包含不属于 $\text{dom}(\text{target.input})$ 的键名，编译期报错。
* **对 `use: builtin:<op>`**（其中 `<op>` 必须属于按逻辑类别划分的 16 个标准内建算子集合）：
  * **标量、赋值与拆包类（2 个）**：`assign`、`unwrap`；
  * **路由、比较、逻辑、算术与长度类（5 个）**：`switch`、`compare`、`logic`、`math`、`length`；
  * **列表操作类（5 个）**：`array_shift`、`array_append`、`array_flatten`、`array_get`、`array_concat`；
  * **有序映射与结构体操作类（4 个）**：`map_set`、`map_get`、`map_delete`、`object_merge`。
  * **`with.input` 约束**：算子签名要求的全部必填 `with.input` 参数必须在 `with.input` 中显式写出，缺失任何必填项或包含未知参数名均在编译期报错。各类算子的入参键名分类如下：
    * 单源输入算子（`assign`、`unwrap`、`switch`、`length`、`array_shift`、`array_flatten`）：必填 `source`；
    * 二元运算算子（`compare`、`logic`、`math`）：必填 `op`、`left`、`right`；其中一元取反 `logic(op: "not")` 仅需 `op` 与 `source`；
    * 列表/映射按位或按键访问与修改算子：必填 `source` 及对应的辅助键（`array_append` 需 `item`；`array_get` 需 `index`；`array_concat` 需 `left` 与 `right`；`map_get` 与 `map_delete` 需 `key`；`map_set` 需 `key` 与 `value`）；
    * 结构体合并算子（`object_merge`）：包含可选保留键 `base` 及至少 1 个目标结构体字段名 $f_i$。
  * **`with.output` 可选性约束**：
    * `builtin:switch` **禁止声明** `with.output`；
    * `builtin:compare` 与 `builtin:logic` 的 `with.output` **可选**（省略 `with.output` 时直接暴露 `.true` / `.false` 控制分支；显式声明 `with.output: { result: <var> }` 时为无分支单出边）；
    * 其余 13 个内建算子的 `with.output` **必填**，且必须显式写出算子签名要求的全部输出键（单值透传/读取算子 `assign`、`unwrap`、`array_get`、`map_get` 为 `value`；容器变换与计算算子为 `result`；`array_shift` 为 `item` + `rest`）。
  * 具体各算子的入参、出参及控制分支契约见 [`05-builtin-generic-inference.md`](../02-type-system-and-symbols/05-builtin-generic-inference.md)。

### 2. `with.input.<param>` 的四态语法解析规则

解析器按 YAML/JSON 值的 AST 节点类型对 `with.input.<param>` 执行确定性消歧：

1. **字符串值（`String`）$\implies$ 变量或静态字段路径引用（`VarOrPathRef`）**：
   * 裸字符串一律解析为 `FieldPathExpr`，在当前解析作用域（若节点未挂载 `for_each` 则为外层图 `variables`；若挂载了 `for_each` 则为单次迭代局部帧）中查找根变量 $\text{RootVar}(E)$。
2. **数值、布尔值或空值（`Integer | Float | Boolean | Null`）$\implies$ 标量常量字面量（`ConstLiteral`）**：
   * 直接解析为传入目标参数 `<param>` 的运行期常量值。
3. **映射对象（`Map`）$\implies$ 显式常量字面量包装器（`WrappedConstLiteral`）**：
   * 若 `with.input.<param>` 的值为 YAML/JSON 映射对象，该对象必须且只能包含唯一键 `value`（即 `{ value: <any_json_literal> }`）；若包含除 `value` 以外的键或为空对象 `{}`，语法解析器报错。
   * `value` 键对应的合法 JSON 值（字符串、数组、对象、数值、布尔值或 `null`）直接作为常量字面量传入目标参数 `<param>`。
4. **裸数组（`Array`）$\implies$ 语法错误**：
   * 数组常量必须通过 `{ value: [...] }` 包装传入。

### 3. `with.output.<target_out_field>` 的显式映射、未绑定默认忽略与单射规则

当 `CallNode`（`use: workflow:<pkg_path>`，无论是否挂载 `for_each`）映射被调用工作流的顶层 `target.output` 时：

* **键与值合法性**：`with.output`（可选，默认 `{}`；当挂载 `for_each` 时要求至少映射 1 个目标输出字段）中的每个键 $f$ 必须满足 $f \in \text{dom}(\text{target.output})$，每个值 `<local_var>` 必须为合法顶层 `RestrictedIdentifier`（**禁止将值设为 `null`**）。
* **显式映射与未列出字段默认忽略**：
  1. 对每个显式列出的条目 `f: <local_var>`，将目标输出字段 $f$ 绑定至调用方图的顶层变量 `<local_var>`（若节点未挂载 `for_each`，`<local_var>` 的类型为单次输出类型 $U = \text{target.output}[f]$；若节点挂载了 `for_each`，`<local_var>` 的类型为经一阶同构集合提升后的类型 $\text{Lift}(T_{\text{src}}, U)$，详见 [03. 单遍显式符号表与模块化类型检查](../02-type-system-and-symbols/03-two-phase-symbol-resolution.md)）；
  2. 对未在 `with.output` 中列出的任何目标输出字段 $f' \in \text{dom}(\text{target.output}) \setminus \text{dom}(N.\text{with.output})$，默认表示**忽略（Ignore / Discard）**：既不要求在调用方符号表 $\Sigma_{\text{caller}}$ 中存在，也不在运行期写回调用方栈帧（被调用方内部出口仍照常执行 `target.output` 的契约校验）。
* **单射约束（Injective Mapping）**：
  $$\forall f_1, f_2 \in \text{dom}(N.\text{with.output}), \quad f_1 \neq f_2 \implies N.\text{with.output}[f_1] \neq N.\text{with.output}[f_2]$$
  即两个不同的目标输出字段禁止映射到同一个调用方图变量 `<local_var>`。
