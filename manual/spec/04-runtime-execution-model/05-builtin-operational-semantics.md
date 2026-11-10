# 05. 内建算子（`builtin:*`）运行期求值语义（Builtin Operational Semantics）

本章节定义全部 16 个内建泛型算子（`use: builtin:<op>`）在运行期激活时的通用求值契约、不可变内存模型，以及各算子对当前栈帧变量环境 $\rho$ 的状态转移方程与出边控制令牌路由规则。

---

## 零、 通用运行期求值契约（General Operational Contracts）

所有 `use: builtin:<op>` 节点均为常数时间（或与浅拷贝集合/结构体长度呈线性正比）、无外部 I/O 副作用、确定性的纯内存转换操作。任意内建算子节点的每一次激活均严格遵循以下三项核心运行期不变式：

### 1. 计步不变式与零可恢复异常保证（Step Accounting & Zero-Exception Guarantee）
* **单步计量**：每次激活未挂载 `for_each` 的 `use: builtin:<op>` 节点 $N$ 前，所属图栈帧 $F_G$ 严格执行单步累加：
  $$\text{steps}(F_G) \leftarrow \text{steps}(F_G) + 1$$
  若挂载了 `for_each`（源集合长度为 $n = |S|$），则每启动第 $i$ 个元素（$0 \le i < n$）的算子求值前累加 $1$ 步（共累加 $n$ 步）。这确保由纯 `builtin:*` 节点构成的循环同样受单图 `max_steps` 护栏保护。
* **永不抛出可恢复运行时异常**：由于编译期静态类型检查（`TypeSpec` 推导）与明确定值分析（Definite Assignment Analysis）已静态证明所有输入操作数已绑定且类型兼容，且所有涉及潜在边界情况的操作（如可空拆包、除零/模零、列表下标越界、映射键缺失、空列表弹出）均通过静态多出口分支（`.some` / `.none` / `.empty`）或 `default` 回退参数显式消解，内建算子在运行期**永不抛出任何可被 `retry` 或 `catch` 捕获的运行时异常**。

### 2. 写时复制纯函数式不可变值语义（Copy-on-Write Immutability）
* 所有涉及集合或结构体更新的内建算子（`builtin:array_shift`、`builtin:array_append`、`builtin:array_concat`、`builtin:array_flatten`、`builtin:map_set`、`builtin:map_delete` 与 `builtin:object_merge`）均遵循**写时复制（Copy-on-Write）纯函数式不可变值语义**。
* 算子求值始终返回新构造的列表、有序映射或结构体副本（或基于结构共享的持久化不可变数据结构），**绝不原地修改（In-place Mutate）当前栈帧变量环境 $\rho$、父栈帧只读快照 $\rho_{\text{parent\_snap}}$ 或任何在途并发分支持有的历史对象引用**。

### 3. 表达式求值函数 $\text{Eval}(\rho, \text{arg})$ 与反定值状态转移（Unbind Transition）
* **输入参数求值函数 $\text{Eval}(\rho, \text{arg})$**：
  对于 `with.input` 中传入的任意参数项 $\text{arg}$，求值函数 $\text{Eval}(\rho, \text{arg})$ 定义如下：
  1. **顶层变量引用**（$\text{arg} \equiv v \in \text{RestrictedIdentifier}$）：直接返回当前栈帧环境中的绑定值 $\rho(v)$（由编译期明确定值分析保证 $v \in \text{dom}(\rho)$ 且 $\rho(v) \neq \text{Unbound}$）。
  2. **静态字段路径投影**（$\text{arg} \equiv v.f_1.f_2\dots f_k \in \text{FieldPathExpr}$）：从根结构体对象 $O_0 = \rho(v)$ 出发，沿路径片段 $f_1, \dots, f_k$ 逐级读取属性 $O_i = O_{i-1}[f_i]$。由于编译期禁止穿越可空（`nullable: true`）或非必填（`required` 未包含）的中间结构体节点，所有中间节点 $O_0, \dots, O_{k-1}$ 在运行期必定为非空对象；若最终叶子属性 $f_k$ 为所属 `TypedDict` 的非必填字段且在运行期实例 $O_{k-1}$ 中缺席（未出现该键），则求值结果规约为 $\texttt{null}$：
     $$\text{Eval}(\rho, v.f_1\dots f_k) = \begin{cases} O_{k-1}[f_k] & \text{若 } f_k \in \text{dom}(O_{k-1}) \\ \texttt{null} & \text{若 } f_k \notin \text{dom}(O_{k-1}) \text{（缺失的非必填叶子属性）} \end{cases}$$
  3. **常量字面量**（$\text{arg} \equiv \text{Literal}(c)$）：直接返回该字面量对应的不可变 JSON 值 $c$。
* **反定值解绑状态转移（$\rho(v) \leftarrow \text{Unbound}$）**：
  当算子在无 `default` 模式下命中缺失或空分支（如 `builtin:unwrap`、`builtin:array_get`、`builtin:map_get`、除零时的 `builtin:math` 走 `.none` 分支，或 `builtin:array_shift` 走 `.empty` 分支）时，运行时必须将当前栈帧环境 $\rho$ 中对应的目标输出变量显式抹除为未绑定状态：
  $$\rho(v_{\text{out}}) \leftarrow \text{Unbound}$$
  这一反定值操作在运行期彻底消除上一轮循环迭代或前置路径遗留的陈旧值（Stale Value），与编译期数据流分析中的反定值杀死集（$\text{Kill}_{\text{edge}}$）严格同构。

---

## 一、 标量赋值与可空拆包算子

### 1. `builtin:assign`（标量/复合值直通赋值与字段投影提取）
* **输入求值**：读取源值 $x = \text{Eval}(\rho, \text{with.input.source})$（支持顶层变量、`FieldPathExpr` 静态字段路径投影或常量字面量），记 `with.output.value` 绑定的目标变量为 $v_{\text{out}}$。
* **状态转移与控制流路由**：
  $$\begin{aligned}
  \rho' &= \rho[v_{\text{out}} \mapsto x] \\
  \text{edge} &= \text{default（唯一无分支出边）}
  \end{aligned}$$

### 2. `builtin:unwrap`（可空类型安全拆包与默认值回退）
* **输入求值**：读取源可空值 $x = \text{Eval}(\rho, \text{with.input.source})$，记 `with.output.value` 绑定的目标变量为 $v_{\text{out}}$。
* **状态转移与控制流路由**：
  * **模式 A（未声明 `with.input.default`）**：
    $$\begin{cases}
    \rho' = \rho[v_{\text{out}} \mapsto x], \quad \text{edge} = \texttt{.some} & \text{若 } x \neq \texttt{null} \\
    \rho' = \rho[v_{\text{out}} \mapsto \text{Unbound}], \quad \text{edge} = \texttt{.none} & \text{若 } x = \texttt{null}
    \end{cases}$$
  * **模式 B（声明了 `with.input.default`）**：
    令回退结果 $y = \begin{cases} x & \text{若 } x \neq \texttt{null} \\ \text{Eval}(\rho, \text{with.input.default}) & \text{若 } x = \texttt{null} \end{cases}$，则：
    $$\begin{aligned}
    \rho' &= \rho[v_{\text{out}} \mapsto y] \\
    \text{edge} &= \text{default（唯一无分支出边）}
    \end{aligned}$$

---

## 二、 条件路由、比较、逻辑与算术算子

### 3. `builtin:switch`（布尔与有限枚举穷尽多路路由）
* **输入求值**：读取判别源值 $x = \text{Eval}(\rho, \text{with.input.source})$。
* **状态转移与控制流路由**：
  该算子不声明 `with.output`，保持变量环境不变（$\rho' = \rho$）：
  * 若源类型为 `boolean`：当 $x = \texttt{true}$ 时控制令牌转入 $\texttt{.true}$ 出边；当 $x = \texttt{false}$ 时转入 $\texttt{.false}$ 出边。
  * 若源类型为有限字符串枚举 `string`（`enum: [e_1, ..., e_k]`）：控制令牌转入与字面量 $x$ 精确同名的分支出边 $\texttt{.}x$。

### 4. `builtin:compare`（二元比较谓词与条件路由）
* **输入求值**：读取比较算子 $op = \text{Eval}(\rho, \text{with.input.op}) \in \{\texttt{"eq"}, \texttt{"ne"}, \texttt{"lt"}, \texttt{"lte"}, \texttt{"gt"}, \texttt{"gte"}\}$，以及左操作数 $x = \text{Eval}(\rho, \text{with.input.left})$、右操作数 $y = \text{Eval}(\rho, \text{with.input.right})$。
* **比较谓词求值 $b \in \{\texttt{true}, \texttt{false}\}$**：
  * **相等性比较（$op \in \{\texttt{"eq"}, \texttt{"ne"}\}$）**：按 JSON 深度结构化值相等（Deep Structural Equality）判定 $x \equiv_{\text{val}} y$：
    * 数值类型跨 `integer` 与 `number` 按精确十进制实数值相等判定（例如 $1 \equiv_{\text{val}} 1.0$）；
    * 字符串按 UTF-8 / Unicode 码点序列精确匹配；
    * 不透明句柄 `handle` 按其闭合传输信封 $\{\texttt{"\$handle"}: h, \texttt{"kind"}: k\}$ 的身份标识 $h$ 与类型标识 $k$ 精确匹配；
    * 列表 `Array<T>` 按长度相等且对应下标元素递归满足 $\equiv_{\text{val}}$ 判定；
    * 结构体 `TypedDict` 与有序映射 `OrderedMap` 按键集合相同且每个键对应的值递归满足 $\equiv_{\text{val}}$ 判定（相等性比较忽略键的插入顺序）；
    * $\texttt{null}$ 仅与 $\texttt{null}$ 相等；
    * 当 $op = \texttt{"eq"}$ 时 $b = (x \equiv_{\text{val}} y)$；当 $op = \texttt{"ne"}$ 时 $b = \lnot(x \equiv_{\text{val}} y)$。
  * **全序大小比较（$op \in \{\texttt{"lt"}, \texttt{"lte"}, \texttt{"gt"}, \texttt{"gte"}\}$）**：
    * 若 $x, y$ 为数值（`integer` / `number`），按精确十进制实数全序关系 $<, \le, >, \ge$ 比较（不经由 IEEE 754 `f64` 截断）；
    * 若 $x, y$ 为字符串（`string`），按 Unicode 码点（Code Points）字典序比较。
* **状态转移与控制流路由**：
  * **模式 A（未声明 `with.output`，直接作为条件路由节点）**：
    $$\rho' = \rho, \quad \text{edge} = \begin{cases} \texttt{.true} & \text{若 } b = \texttt{true} \\ \texttt{.false} & \text{若 } b = \texttt{false} \end{cases}$$
  * **模式 B（声明了 `with.output.result` 绑定目标变量 $v_{\text{res}}$）**：
    $$\rho' = \rho[v_{\text{res}} \mapsto b], \quad \text{edge} = \text{default（唯一无分支出边）}$$

### 5. `builtin:logic`（布尔逻辑组合与取反）
* **输入求值**：读取逻辑算子 $op = \text{Eval}(\rho, \text{with.input.op}) \in \{\texttt{"and"}, \texttt{"or"}, \texttt{"not"}\}$。
* **逻辑布尔值求值 $b \in \{\texttt{true}, \texttt{false}\}$**：
  $$b = \begin{cases}
  \text{Eval}(\rho, \text{with.input.left}) \land \text{Eval}(\rho, \text{with.input.right}) & \text{若 } op = \texttt{"and"} \\
  \text{Eval}(\rho, \text{with.input.left}) \lor \text{Eval}(\rho, \text{with.input.right}) & \text{若 } op = \texttt{"or"} \\
  \lnot \text{Eval}(\rho, \text{with.input.source}) & \text{若 } op = \texttt{"not"}
  \end{cases}$$
* **状态转移与控制流路由**：
  * **模式 A（未声明 `with.output`，直接作为条件路由节点）**：
    $$\rho' = \rho, \quad \text{edge} = \begin{cases} \texttt{.true} & \text{若 } b = \texttt{true} \\ \texttt{.false} & \text{若 } b = \texttt{false} \end{cases}$$
  * **模式 B（声明了 `with.output.result` 绑定目标变量 $v_{\text{res}}$）**：
    $$\rho' = \rho[v_{\text{res}} \mapsto b], \quad \text{edge} = \text{default（唯一无分支出边）}$$

### 6. `builtin:math`（除零安全二元算术运算）
* **输入求值**：读取算术算子 $op = \text{Eval}(\rho, \text{with.input.op}) \in \{\texttt{"add"}, \texttt{"sub"}, \texttt{"mul"}, \texttt{"div"}, \texttt{"mod"}\}$，以及左操作数 $x = \text{Eval}(\rho, \text{with.input.left})$、右操作数 $y = \text{Eval}(\rho, \text{with.input.right})$，记 `with.output.result` 绑定的目标变量为 $v_{\text{res}}$。
* **状态转移与控制流路由**：
  * **情形 1：加、减、乘运算（$op \in \{\texttt{"add"}, \texttt{"sub"}, \texttt{"mul"}\}$）**：
    计算 $z = x + y$（`add`）、$z = x - y$（`sub`）或 $z = x \times y$（`mul`）。当左右操作数的静态类型均为 `integer` 时，$z$ 按任意精度精确整数运算；当任一操作数的静态类型为 `number` 时，$z$ 按 IEEE 754 双精度浮点数（`f64`）运算（若结果溢出为非有限数 `NaN` / `Inf` 则触发不可恢复的引擎级错误）：
    $$\rho' = \rho[v_{\text{res}} \mapsto z], \quad \text{edge} = \text{default（唯一无分支出边）}$$
  * **情形 2：除法与整数取模运算（$op \in \{\texttt{"div"}, \texttt{"mod"}\}$）**：
    当 $y \neq 0$ 时，若 $op = \texttt{"div"}$ 则按 IEEE 754 双精度浮点数（`f64`）计算真除法 $z = x / y$；若 $op = \texttt{"mod"}$ 则按任意精度整数计算向负无穷取整的数学取模（余数与除数 $y$ 同号，即 $z = x - \lfloor x / y \rfloor \cdot y$）：
    * **模式 A（未声明 `with.input.default`）**：
      $$\begin{cases}
      \rho' = \rho[v_{\text{res}} \mapsto z], \quad \text{edge} = \texttt{.some} & \text{若 } y \neq 0 \\
      \rho' = \rho[v_{\text{res}} \mapsto \text{Unbound}], \quad \text{edge} = \texttt{.none} & \text{若 } y = 0\text{（除数为零）}
      \end{cases}$$
    * **模式 B（声明了 `with.input.default`）**：
      令结果 $w = \begin{cases} z & \text{若 } y \neq 0 \\ \text{Eval}(\rho, \text{with.input.default}) & \text{若 } y = 0 \end{cases}$，则：
      $$\rho' = \rho[v_{\text{res}} \mapsto w], \quad \text{edge} = \text{default（唯一无分支出边）}$$

### 7. `builtin:length`（集合与字符串长度测量）
* **输入求值**：读取源对象 $S = \text{Eval}(\rho, \text{with.input.source})$，记 `with.output.result` 绑定的目标变量为 $v_{\text{res}}$。
* **长度计算 $n \in \mathbb{N}$**：
  $$n = \begin{cases}
  |S| \text{（列表元素个数）} & \text{若 } S \text{ 为列表 } \texttt{Array<T>} \\
  |\text{dom}(S)| \text{（映射键值对个数）} & \text{若 } S \text{ 为有序映射 } \texttt{OrderedMap<string, V>} \\
  \text{Unicode 码点（Code Points）字符个数} & \text{若 } S \text{ 为字符串 } \texttt{string}
  \end{cases}$$
* **状态转移与控制流路由**：
  $$\rho' = \rho[v_{\text{res}} \mapsto n], \quad \text{edge} = \text{default（唯一无分支出边）}$$

---

## 三、 强类型列表算子

### 8. `builtin:array_get`（列表按索引安全随机读取）
* **输入求值**：读取源列表 $A = \text{Eval}(\rho, \text{with.input.source}) = [a_0, a_1, \dots, a_{m-1}]$（长度 $m = |A|$）与整数索引 $idx = \text{Eval}(\rho, \text{with.input.index}) \in \mathbb{Z}$，记 `with.output.value` 绑定的目标变量为 $v_{\text{out}}$。
* **负索引归一化**：
  支持从列表尾部倒数寻址，定义归一化下标 $j$ 为：
  $$j = \begin{cases} idx & \text{若 } idx \ge 0 \\ m + idx & \text{若 } idx < 0 \end{cases}$$
* **状态转移与控制流路由**：
  * **模式 A（未声明 `with.input.default`）**：
    $$\begin{cases}
    \rho' = \rho[v_{\text{out}} \mapsto a_j], \quad \text{edge} = \texttt{.some} & \text{若 } 0 \le j < m\text{（未越界）} \\
    \rho' = \rho[v_{\text{out}} \mapsto \text{Unbound}], \quad \text{edge} = \texttt{.none} & \text{若 } j < 0 \lor j \ge m\text{（下标越界，含 } m = 0\text{）}
    \end{cases}$$
  * **模式 B（声明了 `with.input.default`）**：
    令结果 $y = \begin{cases} a_j & \text{若 } 0 \le j < m \\ \text{Eval}(\rho, \text{with.input.default}) & \text{若 } j < 0 \lor j \ge m \end{cases}$，则：
    $$\rho' = \rho[v_{\text{out}} \mapsto y], \quad \text{edge} = \text{default（唯一无分支出边）}$$

### 9. `builtin:array_shift`（列表首元素不可变弹出与空列表判别）
* **输入求值**：读取源列表 $A = \text{Eval}(\rho, \text{with.input.source}) = [a_0, a_1, \dots, a_{m-1}]$（长度 $m = |A|$），记 `with.output.item` 绑定的首元素目标变量为 $v_{\text{item}}$，`with.output.rest` 绑定的剩余列表目标变量为 $v_{\text{rest}}$。
* **状态转移与控制流路由**：
  * **当 $m > 0$（列表非空）时**：构造去除首元素后的新列表副本 $A_{\text{rest}} = [a_1, \dots, a_{m-1}]$（不修改原列表引用），同时更新 $v_{\text{item}}$ 与 $v_{\text{rest}}$：
    $$\rho' = \rho[v_{\text{item}} \mapsto a_0,\; v_{\text{rest}} \mapsto [a_1, \dots, a_{m-1}]], \quad \text{edge} = \texttt{.some}$$
  * **当 $m = 0$（列表为空）时**：将首元素目标变量 $v_{\text{item}}$ 显式反定值解绑（消除上一轮循环或前置节点的旧值），保持 $\rho(v_{\text{rest}})$ 原状态不变（若 $v_{\text{rest}}$ 尚未绑定则维持 $\text{Unbound}$）：
    $$\rho' = \rho[v_{\text{item}} \mapsto \text{Unbound}], \quad \text{edge} = \texttt{.empty}$$

### 10. `builtin:array_append`（列表尾部单元素不可变追加）
* **输入求值**：读取源列表 $A = \text{Eval}(\rho, \text{with.input.source}) = [a_0, \dots, a_{m-1}]$ 与待追加元素 $x = \text{Eval}(\rho, \text{with.input.item})$，记 `with.output.result` 绑定的目标变量为 $v_{\text{res}}$。
* **状态转移与控制流路由**：
  构造包含原列表全部元素并在尾部追加 $x$ 的新列表副本 $A' = [a_0, \dots, a_{m-1}, x]$：
  $$\rho' = \rho[v_{\text{res}} \mapsto [a_0, \dots, a_{m-1}, x]], \quad \text{edge} = \text{default（唯一无分支出边）}$$

### 11. `builtin:array_concat`（同构列表不可变拼接）
* **输入求值**：读取左源列表 $A_L = \text{Eval}(\rho, \text{with.input.left}) = [a_0, \dots, a_{m-1}]$ 与右源列表 $A_R = \text{Eval}(\rho, \text{with.input.right}) = [b_0, \dots, b_{n-1}]$，记 `with.output.result` 绑定的目标变量为 $v_{\text{res}}$。
* **状态转移与控制流路由**：
  按序构造包含 $A_L$ 全部元素后接 $A_R$ 全部元素的新列表副本 $R = A_L \mathbin{\Vert} A_R = [a_0, \dots, a_{m-1}, b_0, \dots, b_{n-1}]$：
  $$\rho' = \rho[v_{\text{res}} \mapsto A_L \mathbin{\Vert} A_R], \quad \text{edge} = \text{default（唯一无分支出边）}$$

### 12. `builtin:array_flatten`（二维列表单层保序展平）
* **输入求值**：读取源二维列表 $A = \text{Eval}(\rho, \text{with.input.source}) = [L_0, L_1, \dots, L_{m-1}]$（其中每个元素 $L_i = [w_{i,0}, \dots, w_{i,k_i-1}]$ 为内层子列表），记 `with.output.result` 绑定的目标变量为 $v_{\text{res}}$。
* **状态转移与控制流路由**：
  严格按外层索引 $i = 0, \dots, m-1$ 的顺序与各子列表 $L_i$ 的内部元素顺序执行单层首尾拼接（仅展平最外层二维结构，若元素本身仍含嵌套集合则保持原样），构造新的一维列表副本 $R = L_0 \mathbin{\Vert} L_1 \mathbin{\Vert} \dots \mathbin{\Vert} L_{m-1}$（当 $m = 0$ 或所有子列表均为空时，$R = []$）：
  $$\rho' = \rho[v_{\text{res}} \mapsto L_0 \mathbin{\Vert} L_1 \mathbin{\Vert} \dots \mathbin{\Vert} L_{m-1}], \quad \text{edge} = \text{default（唯一无分支出边）}$$

---

## 四、 有序映射与强类型结构体算子

### 13. `builtin:map_get`（有序映射按键安全查找）
* **输入求值**：读取源有序映射 $M = \text{Eval}(\rho, \text{with.input.source}) = [(k_0, u_0), \dots, (k_{m-1}, u_{m-1})]$ 与目标查找键 $k = \text{Eval}(\rho, \text{with.input.key}) \in \text{String}$，记 `with.output.value` 绑定的目标变量为 $v_{\text{out}}$。
* **状态转移与控制流路由**：
  * **模式 A（未声明 `with.input.default`）**：
    $$\begin{cases}
    \rho' = \rho[v_{\text{out}} \mapsto u_j], \quad \text{edge} = \texttt{.some} & \text{若 } \exists j \in \{0, \dots, m-1\}\text{ 使得 } k_j = k \\
    \rho' = \rho[v_{\text{out}} \mapsto \text{Unbound}], \quad \text{edge} = \texttt{.none} & \text{若 } k \notin \text{dom}(M)
    \end{cases}$$
  * **模式 B（声明了 `with.input.default`）**：
    令结果 $y = \begin{cases} u_j & \text{若 } \exists j \text{ 使得 } k_j = k \\ \text{Eval}(\rho, \text{with.input.default}) & \text{若 } k \notin \text{dom}(M) \end{cases}$，则：
    $$\rho' = \rho[v_{\text{out}} \mapsto y], \quad \text{edge} = \text{default（唯一无分支出边）}$$

### 14. `builtin:map_set`（有序映射不可变插入/更新与键序保持不变式）
* **输入求值**：读取源有序映射 $M = \text{Eval}(\rho, \text{with.input.source}) = [(k_0, u_0), \dots, (k_{m-1}, u_{m-1})]$、目标键 $k = \text{Eval}(\rho, \text{with.input.key})$ 与目标值 $v = \text{Eval}(\rho, \text{with.input.value})$，记 `with.output.result` 绑定的目标变量为 $v_{\text{res}}$。
* **`OrderedMap` 键序保持规则**：
  构造新的有序映射副本 $M'$（不原地修改 $M$）：
  $$M' = \begin{cases}
  [(k_0, u_0), \dots, (k_{j-1}, u_{j-1}), (k_j, v), (k_{j+1}, u_{j+1}), \dots, (k_{m-1}, u_{m-1})] & \text{若 } \exists j \text{ 使得 } k_j = k\text{（原位更新值，保持 } k \text{ 原有插入位置不变）} \\
  [(k_0, u_0), \dots, (k_{m-1}, u_{m-1}), (k, v)] & \text{若 } k \notin \text{dom}(M)\text{（新键值对追加至遍历序列末尾）}
  \end{cases}$$
* **状态转移与控制流路由**：
  $$\rho' = \rho[v_{\text{res}} \mapsto M'], \quad \text{edge} = \text{default（唯一无分支出边）}$$

### 15. `builtin:map_delete`（有序映射不可变按键删除与相对键序保持）
* **输入求值**：读取源有序映射 $M = \text{Eval}(\rho, \text{with.input.source}) = [(k_0, u_0), \dots, (k_{m-1}, u_{m-1})]$ 与待移除的目标键 $k = \text{Eval}(\rho, \text{with.input.key})$，记 `with.output.result` 绑定的目标变量为 $v_{\text{res}}$。
* **`OrderedMap` 键序保持删除**：
  构造移除键 $k$ 后的新有序映射副本 $M'$，严格保持剩余键原有的相对插入顺序不变（若 $k \notin \text{dom}(M)$，则返回与 $M$ 具有相同键值对序列的浅拷贝副本）：
  $$M' = [(k_i, u_i) \in M \mid k_i \neq k]$$
* **状态转移与控制流路由**：
  $$\rho' = \rho[v_{\text{res}} \mapsto M'], \quad \text{edge} = \text{default（唯一无分支出边）}$$

### 16. `builtin:object_merge`（强类型结构体 `TypedDict` 不可变构造与字段浅层合并）
* **输入求值**：记 `with.input` 中除保留键 `base` 以外显式声明的字段更新集合为 $\{f_1, f_2, \dots, f_m\}$（$m \ge 1$），记 `with.output.result` 绑定的目标变量为 $v_{\text{res}}$。
* **不可变结构体副本构造与属性覆盖**：
  1. **基底初始化**：
     * 若声明了 `with.input.base`，读取源结构体并构造浅拷贝副本 $O_0 = \text{Clone}(\text{Eval}(\rho, \text{with.input.base}))$（保留源结构体中全部已有字段键值，且不修改源引用）；
     * 若未声明 `with.input.base`（从零构造完整结构体），初始化空记录对象 $O_0 = \{\}$。
  2. **字段逐一求值与覆盖**：
     对每个声明字段 $f_i \in \{f_1, \dots, f_m\}$，求值 $v_i = \text{Eval}(\rho, \text{with.input}[f_i])$ 并写入副本属性 $O'[f_i] \leftarrow v_i$：
     $$O' = O_0[f_1 \mapsto \text{Eval}(\rho, \text{with.input}[f_1]),\; \dots,\; f_m \mapsto \text{Eval}(\rho, \text{with.input}[f_m])]$$
* **状态转移与控制流路由**：
  $$\rho' = \rho[v_{\text{res}} \mapsto O'], \quad \text{edge} = \text{default（唯一无分支出边）}$$
