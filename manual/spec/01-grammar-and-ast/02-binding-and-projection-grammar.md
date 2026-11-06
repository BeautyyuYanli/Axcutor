# 02. 数据绑定、`with` 映射与静态字段路径文法规范

本规范定义变量标识符、静态字段路径投影表达式（`FieldPathExpr`）以及 `use` 节点中 `with` 映射的词法与语法消歧规则。

---

## 一、 标识符与静态字段路径词法规则

1. **变量标识符（`Identifier`）**：
   $$\text{Identifier} ::= \text{[a-zA-Z\_][a-zA-Z0-9\_]*}$$
   严禁包含点号 `.`、空格或连字符 `-`。
2. **静态字段路径表达式（`FieldPathExpr`）**：
   $$\text{FieldPathExpr} ::= v_0 (\text{"."} f_i)^* \quad (i \ge 0)$$
   * 当 $k = 0$ 时，退化为顶层根变量引用 $v_0$；
   * 当 $k \ge 1$ 时，$E = v_0 . f_1 . f_2 \dots f_k$ 表示对根变量 $\text{RootVar}(E) = v_0$ 的 $k$ 级静态字段路径投影。

---

## 二、 允许出现 `FieldPathExpr` 与仅限顶层 `Identifier` 的语法位置

为了确保图级变量表（`variables`）的写入始终保持顶层原子性（无部分字段原地可变副作用），语法解析器严格区分只读位置与写入/捕获位置：

| 语法位置 | 允许的表达式形态 | 规范约束 |
| :--- | :--- | :--- |
| **`for_each.source`** | `FieldPathExpr` ($k \ge 0$) | 支持绑定顶层变量名或非空 `TypedDict` 的嵌套字段路径。 |
| **`use.with.input.<param>`**（当值为字符串时） | `FieldPathExpr` ($k \ge 0$) | 支持绑定顶层变量名或非空 `TypedDict` 的嵌套字段路径。 |
| **`run` 内部变量引用** | `variables.` + `FieldPathExpr` | 其根变量 $\text{RootVar}(E)$ **必须已显式包含在所属 `run` 节点的 `input: [...]` 列表中**，否则编译期直接报错。 |
| **`run.input` / `graph.input`** | 仅限顶层 `Identifier` ($k = 0$) | 严禁出现点号 `.`。 |
| **`output` / `with.output.<field>`** | 仅限顶层 `Identifier` ($k = 0$) | 严禁出现点号 `.`。 |
| **`for_each.item` / `key` / `index`** | 仅限顶层 `Identifier` ($k = 0$) | 严禁出现点号 `.`。 |
| **`for_each.filter.*` / `collect.*` / `state`** | 仅限顶层 `Identifier` ($k = 0$) | 严禁出现点号 `.`。 |

---

## 三、 `use.with` 绑定语法与 `{ value: <literal> }` 消歧规则

在 `CallNode`（`use: workflow:<path>` 或 `use: builtin:<op>`）中，`with` 包含可选的 `input` 映射与 `output` 映射：

### 1. `with.input.<param>` 的四态语法解析规则

解析器按 YAML/JSON 值的 AST 节点类型对 `with.input.<param>` 执行确定性消歧：

1. **字符串值（`String`）$\implies$ 变量或静态字段路径引用（`VarOrPathRef`）**：
   * 任何裸字符串一律解析为 `FieldPathExpr`，在当前解析作用域（若节点未挂载 `for_each` 则为外层图 `variables`；若挂载了 `for_each` 则为单次迭代局部帧）中查找根变量 $\text{RootVar}(E)$。
2. **数值、布尔值或空值（`Integer | Float | Boolean | Null`）$\implies$ 标量常量字面量（`ConstLiteral`）**：
   * 直接解析为传入目标参数 `<param>` 的运行期常量值。
3. **映射对象（`Map`）$\implies$ 显式常量字面量包装器（`WrappedConstLiteral`）**：
   * 若 `with.input.<param>` 的值为 YAML/JSON 映射对象，**该对象必须且只能包含唯一键 `value`**（即 `{ value: <any_json_literal> }`）；若包含除 `value` 以外的键或为空对象 `{}`，语法解析器直接报错。
   * `value` 键对应的任意合法 JSON 值（字符串、数组、对象、数值、布尔值或 `null`）直接作为常量字面量传入目标参数 `<param>`。
4. **裸数组（`Array`）$\implies$ 语法错误**：
   * 为保持复合字面量语法一致性，数组常量必须通过 `{ value: [...] }` 包装传入。

### 2. `with.output.<target_out_field>` 的单射映射规则

* `with.output` 键 `<target_out_field>` 必须属于被调用目标 `output` 声明的字段集合；值必须为当前图的顶层变量标识符 `<local_var>`。
* 省略 `with.output`（或省略某个输出字段）时，默认按恒等映射 `<out_field>: <out_field>` 处理。
* **单射约束（Injective Mapping）**：生效的完整输出映射表中，不同的目标输出字段严禁映射到同一个当前图变量名 `<local_var>`。
