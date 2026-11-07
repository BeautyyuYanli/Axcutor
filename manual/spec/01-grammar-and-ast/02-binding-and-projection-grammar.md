# 02. 数据绑定、`with` 映射与静态字段路径文法规范

本规范定义变量标识符、静态字段路径投影表达式（`FieldPathExpr`）、`run` 字符串内变量引用的词法边界提取规则，以及 `use` 节点中 `with` 映射的语法消歧、默认补全、显式丢弃与 `for_each` 交互契约。

---

## 一、 标识符与静态字段路径词法规则

1. **变量标识符（`Identifier`）**：
   $$\text{Identifier} ::= \text{[a-zA-Z\_][a-zA-Z0-9\_]*}$$
   禁止包含点号 `.`、空格或连字符 `-`。
2. **静态字段路径表达式（`FieldPathExpr`）**：
   $$\text{FieldPathExpr} ::= v_0 (\text{"."} f_i)^* \quad (i \ge 0)$$
   * 当 $k = 0$ 时，退化为顶层根变量引用 $v_0$；
   * 当 $k \ge 1$ 时，$E = v_0 . f_1 . f_2 \dots f_k$ 表示对根变量 $\text{RootVar}(E) = v_0$ 的 $k$ 级静态字段路径投影。
3. **`run` 字符串中的变量引用词法边界规则**：
   * 当 `run` 执行体为字符串时，静态检查器按以下最长匹配正则模式提取所有以 `variables.` 开头的字段路径表达式 $E$：
     $$\text{RunVarRef} ::= \texttt{\textbackslash bvariables\textbackslash .}\text{([a-zA-Z\_][a-zA-Z0-9\_]*(?:\textbackslash .[a-zA-Z\_][a-zA-Z0-9\_]*)*)}$$
   * **句尾标点自动剥离**：由于每一级 `.` 之后必须紧跟合法标识符首字符 `[a-zA-Z_]`，自然语言文本中紧跟在引用末尾的英文句号或其他标点（如 `"读取 variables.user.name."`）不匹配下一级字段，自动作为普通文本标点剥离，提取出的 `FieldPathExpr` 为 `user.name`。

---

## 二、 允许出现 `FieldPathExpr` 与仅限顶层 `Identifier` 的语法位置

为确保图级变量表（`variables`）的写入保持顶层原子性（无部分字段原地可变副作用），语法解析器严格区分只读位置与写入/捕获位置：

| 语法位置 | 允许的表达式形态 | 规范约束 |
| :--- | :--- | :--- |
| **`for_each.source`** | `FieldPathExpr` ($k \ge 0$) | 支持绑定顶层变量名或非空 `TypedDict` 的嵌套字段路径。 |
| **`use.with.input.<param>`**（当值为字符串时） | `FieldPathExpr` ($k \ge 0$) | 支持绑定顶层变量名或非空 `TypedDict` 的嵌套字段路径。 |
| **`run` 内部变量引用** | `variables.` + `FieldPathExpr` | • 当所属节点为图内 `ActionNode` 时，根变量 $\text{RootVar}(E)$ 必须已显式包含在该节点的 `input: [...]` 列表中（$\text{RootVar}(E) \in \text{set}(N.\text{input})$）；<br>• 当所属节点为顶层 `RootRunNode` 时，根变量必须属于顶层输入契约的键集合（$\text{RootVar}(E) \in \text{dom}(\text{root.input})$）；<br>否则编译期报错。 |
| **`run.input` / `graph.input`** | 仅限顶层 `Identifier` ($k = 0$) | 禁止出现点号 `.`。 |
| **`output` / `with.output.<field>`** | 仅限顶层 `Identifier` ($k = 0$) 或 `null`（仅限 `with.output`） | 禁止出现点号 `.`。 |
| **`for_each.item` / `key` / `index`** | 仅限顶层 `Identifier` ($k = 0$) | 禁止出现点号 `.`。 |
| **`for_each.filter.*` / `collect.*` / `state`** | 仅限顶层 `Identifier` ($k = 0$) | 禁止出现点号 `.`。 |

---

## 三、 `use.with` 绑定语法、`{ value: <literal> }` 消歧与输出映射规则

在 `CallNode`（`use: workflow:<path>` 或 `use: builtin:<op>`）中，`with` 包含可选的 `input` 映射与 `output` 映射。

### 1. `with.input` 默认同名补全与必填显式约束

1. **`use: workflow:<path>` 的默认同名顶层变量绑定**：
   * 设被调用子工作流顶层声明的输入契约表为 $\text{target.input}$。允许调用方省略 `with`、省略 `with.input` 或仅在 `with.input` 中声明部分参数；
   * 对于任意目标输入参数 $p \in \text{dom}(\text{target.input})$，若 $p \notin \text{dom}(N.\text{with.input})$，编译器在 AST 规范化阶段自动将其补全为对调用方当前作用域**同名顶层变量**的引用：
     $$N.\text{with.input}[p] \leftarrow \text{VarOrPathRef}(p)$$
   * 若 `with.input` 中出现了不属于 $\text{dom}(\text{target.input})$ 的键名，编译期报错。
2. **`use: builtin:<op>` 的必填参数显式声明约束**：
   * 内建算子**不支持 `with.input` 或 `with.output` 的隐式同名省略**；
   * 算子签名要求的全部必填 `with.input` 参数（如 `source`、`item`、`key`、`value`；对 `builtin:object_merge` 为可选保留键 `base` 及至少 1 个目标结构体字段名 $f_i$）与必填 `with.output` 参数（如 `value`、`result`、`item`、`rest`）必须在 `with` 中显式写出，缺失任何必填项或包含未知参数名均在编译期报错（具体算子参数契约见 [`05-builtin-generic-inference.md`](../02-type-system-and-symbols/05-builtin-generic-inference.md)）。

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

### 3. `with.output.<target_out_field>` 的映射、显式丢弃（`null`）与单射规则

当未挂载 `for_each` 的 `CallNode`（`use: workflow:<path>`）映射被调用工作流的顶层 `target.output` 时：

* **键合法性**：`with.output` 中的每个键 $f$ 必须满足 $f \in \text{dom}(\text{target.output})$，否则编译期报错。
* **值形态与三态语义**：对于每个目标输出字段 $f \in \text{dom}(\text{target.output})$：
  1. **显式绑定顶层变量（`f: <local_var>`）**：值为合法顶层 `Identifier` 时，将目标输出字段 $f$ 映射至调用方图的顶层变量 `<local_var>`；
  2. **省略时默认恒等映射（Omitted $\implies f \mapsto f$）**：当完全省略 `with.output` 或未在 `with.output` 中列出字段 $f$ 时，默认补全为同名顶层变量映射 $f: f$；
  3. **显式丢弃未用输出（`f: null`）**：当值显式设为 `null` 时，表示**丢弃（Discard）**被调用方产出的字段 $f$。被丢弃的字段：
     * **不注入调用方符号表 $\Sigma_{\text{caller}}$**（避免污染调用方命名空间或与调用方已有的同名异构局部变量触发类型冲突）；
     * **不在运行期写回调用方栈帧**（仍执行被调用方内部的出口 `TypeSpec` 契约校验，但提交阶段忽略该字段）。
* **单射约束（Injective Mapping on Non-Null Targets）**：
  在补全默认映射并剔除所有值为 `null` 的条目后，定义生效的输出映射函数 $M_{\text{out}} : F_{\text{kept}} \to \text{Identifier}$（其中 $F_{\text{kept}} = \{ f \in \text{dom}(\text{target.output}) \mid \text{with.output}[f] \neq \texttt{null} \}$）。$M_{\text{out}}$ 必须满足单射条件：
  $$\forall f_1, f_2 \in F_{\text{kept}}, \quad f_1 \neq f_2 \implies M_{\text{out}}(f_1) \neq M_{\text{out}}(f_2)$$
  即两个不同的目标输出字段禁止映射到同一个调用方图变量 `<local_var>`。

### 4. `CallNode` 挂载 `for_each` 时的输出对接契约

当 `CallNode`（`use: workflow:<path>`）同时挂载了 `for_each` 修饰符时：

1. **禁止声明 `with.output`**：
   * 节点若在挂载 `for_each` 的同时声明了 `with.output`，编译期直接报错。
2. **由 `for_each.collect` 与 `for_each.state` 直接对接 `target.output`**：
   * 每次元素迭代调用子工作流产出的字段保留其原始名称 $f \in \text{dom}(\text{target.output})$ 存入迭代局部槽位；
   * `for_each.collect` 中的键名直接引用被调用工作流的原始输出字段名 $f \in \text{dom}(\text{target.output})$，并将其提升写入外层图的集合变量 `<target_var>`；
   * `for_each.state` 中的状态变量名 $s$ 必须同时存在于外层图符号表 $\Sigma_{\text{caller}}$ 与被调用工作流的 `target.output` 中（且类型静态等价），未在 `for_each.collect` 或 `for_each.state` 中引用的其余 `target.output` 字段在迭代结束后自动丢弃。
