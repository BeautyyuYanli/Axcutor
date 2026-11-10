# 05. 内建算子（`builtin:*`）编译期泛型类型检查规则

所有 `use: builtin:<op>` 节点在编译期基于已构建完毕的图级符号表 $\Sigma$ 执行静态泛型类型检查（Generic Type Checking）。所有出参绑定的目标顶层变量 $v_{\text{out}}$ 均必须已存在于 $\text{dom}(\Sigma)$ 中，且入参与出参流转位置统一遵循 [`02-type-equivalence-and-compatibility.md`](./02-type-equivalence-and-compatibility.md) 定义的单向静态子类型偏序关系（$<:$）。定义算子 $\text{StripNull}(T)$ 为将显式可空类型 $T = [\text{BaseType}, \texttt{"null"}]$ 移除 $\texttt{"null"}$ 并保留其余全部约束字段后的非空 `TypeSpec`（若 $T$ 本身非空，则 $\text{StripNull}(T) = T$）。

---

## 一、 标量赋值与可空拆包

### 1. `builtin:assign`
* **入参与出参类型校验**：
  * `source`（必填）：支持绑定顶层变量、`FieldPathExpr` 静态字段路径或常量字面量（含数值/布尔/`null` 标量字面量与 `{ value: <literal> }` 包装字面量）。
  * `value`（必填，绑定顶层变量 $v_{\text{out}}$）：要求 $v_{\text{out}} \in \text{dom}(\Sigma)$，记目标类型为 $T_{\text{target}} = \Sigma(v_{\text{out}})$：
    1. **当 `with.input.source` 为变量或静态字段路径引用 $E$ 时**：
      校验静态子类型兼容 $\text{TypeOf}(\Sigma, E) <: T_{\text{target}}$。
    2. **当 `with.input.source` 为常量字面量 $L$ 时**：
      直接校验实例兼容性 $L \models T_{\text{target}}$。
* **静态控制分支推导**：
  * 无分支单出边（$\text{OutVars} = \{v_{\text{out}}\}$）。

### 2. `builtin:unwrap`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须满足 $\text{Nullable}(T_{\text{src}}) = \texttt{true}$，否则报编译期错误（禁止对非空类型冗余拆包）。令非空内层类型 $T = \text{StripNull}(T_{\text{src}})$。
  * `default`（可选）：若声明，当为变量或路径引用 $E_{\text{def}}$ 时，须满足 $\text{TypeOf}(\Sigma, E_{\text{def}}) <: \Sigma(v_{\text{out}})$；当为常量字面量 $L_{\text{def}}$ 时，须满足 $L_{\text{def}} \neq \texttt{null}$ 且 $L_{\text{def}} \models \Sigma(v_{\text{out}})$。
* **出参校验与静态约束**：
  * `value`（必填，绑定顶层变量 $v_{\text{out}}$）：要求 $v_{\text{out}} \in \text{dom}(\Sigma)$、目标变量本身为非空类型 $\text{Nullable}(\Sigma(v_{\text{out}})) = \texttt{false}$，且满足子类型兼容 $T <: \Sigma(v_{\text{out}})$。
  * **根变量异名约束**：$v_{\text{out}} \neq \text{RootVar}(\text{with.input.source})$（因源变量可空而输出变量非空，同一栈帧内不可同名不同类型）。
* **静态控制分支与反定值（Kill）契约推导**：
  * 未声明 `default` 时：暴露 `.some`（$\text{OutVars} = \{v_{\text{out}}\}, \; \text{KillVars} = \emptyset$）与 `.none`（$\text{OutVars} = \emptyset, \; \text{KillVars} = \{v_{\text{out}}\}$，详见 [`03-branch-sensitive-definite-assignment.md`](../03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)）；
  * 声明了 `default` 时：无分支单出边（$\text{OutVars} = \{v_{\text{out}}\}$）。

---

## 二、 条件路由、比较、逻辑与算术运算

### 3. `builtin:switch`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须满足 $\text{Nullable}(T_{\text{src}}) = \texttt{false}$，且属于以下二者之一：
    1. $\text{BaseType}(T_{\text{src}}) = \texttt{boolean}$；
    2. $\text{BaseType}(T_{\text{src}}) = \texttt{string}$ 且声明了非空 `enum: [e_1, ..., e_m]`，同时每个枚举元素必须为合法的分支标识符（`BranchId`）：
       $$\forall e_i \in T_{\text{src}}.\text{enum}, \quad e_i \text{ 匹配 } \texttt{^[a-zA-Z\_][a-zA-Z0-9\_]*\$} \;\land\; e_i \notin \{\texttt{"start"}, \texttt{"end"}, \texttt{"fail"}\}$$
       若存在任何枚举值不是合法的 `BranchId`，静态检查器在编译期报错。
* **出参约束**：禁止声明 `with.output`。
* **静态控制分支推导**：
  * 当为 `boolean` 时：暴露 `.true` 与 `.false`（$\text{OutVars} = \emptyset$）；
  * 当为 `string` + `enum` 时：暴露 `.e_1`, ..., `.e_m`（$\text{OutVars} = \emptyset$）。

### 4. `builtin:compare`
* **入参校验**：
  * `op`（必填）：必须为字符串常量字面量 `{ value: "<op>" }`，取值属于 $\{\texttt{"eq"}, \texttt{"ne"}, \texttt{"lt"}, \texttt{"lte"}, \texttt{"gt"}, \texttt{"gte"}\}$；
  * `left`、`right`（必填）：每个操作数支持绑定变量/静态字段路径引用 $E$（类型为 $\text{TypeOf}(\Sigma, E)$）或常量字面量 $L$。记两操作数的静态类型为 $T_L, T_R$（若某操作数为非空标量常量字面量 $L$，其对应最窄标量类型记为 $\{ \texttt{type}: \text{BaseTypeOf}(L) \}$）：
    1. **当 `op` $\in \{\texttt{"eq"}, \texttt{"ne"}\}$（相等性比较）**：
       * 若一侧为引用表达式（类型为 $T$）且另一侧为 `{ value: null }`（或裸 `null`）字面量，则要求 $\text{Nullable}(T) = \texttt{true}$；
       * 若一侧为引用表达式（类型为 $T$）且另一侧为非 `null` 常量字面量 $L$，则要求 $L \models \text{StripNull}(T)$（或当 $\text{BaseType}(\text{StripNull}(T)) \in \{\texttt{integer}, \texttt{number}\}$ 且 $L$ 为数值字面量时均视为可比）；
       * 若两侧均为引用表达式（类型为 $T_L, T_R$），则必须满足**可比性（Comparable）**：
         $$\text{StripNull}(T_L) <: \text{StripNull}(T_R) \;\lor\; \text{StripNull}(T_R) <: \text{StripNull}(T_L)$$
         *(允许同构标量、相同 `kind` 的不透明句柄 `handle`、列表、结构体或有序映射之间按 JSON 深度值相等比较，且允许可空类型与对应的非空类型或可空类型直接判等。)*
    2. **当 `op` $\in \{\texttt{"lt"}, \texttt{"lte"}, \texttt{"gt"}, \texttt{"gte"}\}$（序比较）**：
       要求两侧均非空（$\text{Nullable}(T_L) = \texttt{false} \;\land\; \text{Nullable}(T_R) = \texttt{false}$，禁止传入 `null`），且满足以下二者之一：
       * **数值序比较**：$\text{BaseType}(T_L), \text{BaseType}(T_R) \in \{\texttt{integer}, \texttt{number}\}$；
       * **字符串字典序比较**：$\text{BaseType}(T_L) = \text{BaseType}(T_R) = \texttt{string}$。
* **出参校验与静态控制分支推导**：
  * **情况 A（省略 `with.output`）**：直接暴露 `.true` 与 `.false` 两个分支（$\text{OutVars} = \emptyset$）；
  * **情况 B（声明 `with.output.result`，绑定顶层变量 $v_{\text{res}}$）**：要求 $v_{\text{res}} \in \text{dom}(\Sigma)$ 且满足 $\{ \texttt{type}: \texttt{boolean} \} <: \Sigma(v_{\text{res}})$，无分支单出边（$\text{OutVars} = \{v_{\text{res}}\}$）。

### 5. `builtin:logic`
* **入参校验**：
  * `op`（必填）：必须为字符串常量字面量 `{ value: "<op>" }`，取值属于 $\{\texttt{"and"}, \texttt{"or"}, \texttt{"not"}\}$；
  * **当 `op` $\in \{\texttt{"and"}, \texttt{"or"}\}$ 时**：必填 `left` 与 `right`（禁止声明 `source`），两者均必须为非空布尔类型（引用表达式满足 $\text{TypeOf}(\Sigma, E) <: \{ \texttt{type}: \texttt{boolean} \}$，字面量满足 $L \in \{\texttt{true}, \texttt{false}\}$）；
  * **当 `op` $= \texttt{"not"}$ 时**：必填 `source`（禁止声明 `left` 或 `right`），必须为非空布尔类型（引用表达式满足 $\text{TypeOf}(\Sigma, E) <: \{ \texttt{type}: \texttt{boolean} \}$，字面量满足 $L \in \{\texttt{true}, \texttt{false}\}$）。
* **出参校验与静态控制分支推导**：
  * **情况 A（省略 `with.output`）**：直接暴露 `.true` 与 `.false` 两个分支（$\text{OutVars} = \emptyset$）；
  * **情况 B（声明 `with.output.result`，绑定顶层变量 $v_{\text{res}}$）**：要求 $v_{\text{res}} \in \text{dom}(\Sigma)$ 且满足 $\{ \texttt{type}: \texttt{boolean} \} <: \Sigma(v_{\text{res}})$，无分支单出边（$\text{OutVars} = \{v_{\text{res}}\}$）。

### 6. `builtin:math`
* **入参校验**：
  * `op`（必填）：必须为字符串常量字面量 `{ value: "<op>" }`，取值属于 $\{\texttt{"add"}, \texttt{"sub"}, \texttt{"mul"}, \texttt{"div"}, \texttt{"mod"}\}$；
  * `left`、`right`（必填）：设其类型为 $T_L, T_R$（支持变量/静态字段路径引用或数值常量字面量），必须满足 $\text{Nullable}(T_L) = \texttt{false} \;\land\; \text{Nullable}(T_R) = \texttt{false}$：
    * 当 `op` $\in \{\texttt{"add"}, \texttt{"sub"}, \texttt{"mul"}, \texttt{"div"}\}$ 时：$\text{BaseType}(T_L), \text{BaseType}(T_R) \in \{\texttt{integer}, \texttt{number}\}$；
    * 当 `op` $= \texttt{"mod"}$ 时：要求两侧均为整数 $\text{BaseType}(T_L) = \text{BaseType}(T_R) = \texttt{integer}$。
  * **结果数值类型 $T_{\text{math}}$**：
    * 当 `op` $\in \{\texttt{"add"}, \texttt{"sub"}, \texttt{"mul"}, \texttt{"mod"}\}$ 且 $\text{BaseType}(T_L) = \text{BaseType}(T_R) = \texttt{integer}$ 时，$T_{\text{math}} = \{ \texttt{type}: \texttt{integer} \}$；
    * 当 `op` $= \texttt{"div"}$（浮点真除法）或任一操作数为 `number` 时，$T_{\text{math}} = \{ \texttt{type}: \texttt{number} \}$。
  * `default`（可选，**仅当 `op` $\in \{\texttt{"div"}, \texttt{"mod"}\}$ 时允许声明**；当 `op` $\in \{\texttt{"add"}, \texttt{"sub"}, \texttt{"mul"}\}$ 时若声明 `default` 则编译期报错）：若声明，当为引用表达式 $E_{\text{def}}$ 时须满足 $\text{TypeOf}(\Sigma, E_{\text{def}}) <: \Sigma(v_{\text{res}})$，当为常量字面量 $L_{\text{def}}$ 时须满足 $L_{\text{def}} \models \Sigma(v_{\text{res}})$。
* **出参校验与静态控制分支推导**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：要求 $v_{\text{res}} \in \text{dom}(\Sigma)$ 且满足子类型兼容 $T_{\text{math}} <: \Sigma(v_{\text{res}})$；
  * **当 `op` $\in \{\texttt{"add"}, \texttt{"sub"}, \texttt{"mul"}\}$ 或声明了 `default` 时**：无分支单出边（$\text{OutVars} = \{v_{\text{res}}\}$，允许 $v_{\text{res}}$ 与 `left` 或 `right` 的根变量同名以实现原地自增/累加更新）；
  * **当 `op` $\in \{\texttt{"div"}, \texttt{"mod"}\}$ 且未声明 `default` 时**：
    * **根变量异名约束**：若 `left` 为引用表达式则 $v_{\text{res}} \neq \text{RootVar}(\text{with.input.left})$，若 `right` 为引用表达式则 $v_{\text{res}} \neq \text{RootVar}(\text{with.input.right})$；
    * 暴露 `.some`（除数非零且结果为有限数，$\text{OutVars} = \{v_{\text{res}}\}, \; \text{KillVars} = \emptyset$）与 `.none`（除数为 `0`，$\text{OutVars} = \emptyset, \; \text{KillVars} = \{v_{\text{res}}\}$，详见 [`03-branch-sensitive-definite-assignment.md`](../03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)）。

### 7. `builtin:length`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须满足 $\text{Nullable}(T_{\text{src}}) = \texttt{false}$，且 $T_{\text{src}}$ 属于以下三者之一：
    1. 强类型列表 `Array<T>`（$\text{BaseType}(T_{\text{src}}) = \texttt{array}$）；
    2. 强类型有序映射 `OrderedMap<string, V>`（$\text{BaseType}(T_{\text{src}}) = \texttt{object}$ 且声明了 `additionalProperties`）；
    3. 字符串标量（$\text{BaseType}(T_{\text{src}}) = \texttt{string}$）。
    *(禁止对固定属性结构体 `TypedDict`、`integer`、`number` 或 `boolean` 求长度，否则编译期报错。)*
* **出参校验与静态控制分支推导**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：要求 $v_{\text{res}} \in \text{dom}(\Sigma)$ 且满足 $\{ \texttt{type}: \texttt{integer} \} <: \Sigma(v_{\text{res}})$；
  * 无分支单出边（$\text{OutVars} = \{v_{\text{res}}\}$）。

---

## 三、 强类型列表算子

### 8. `builtin:array_get`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须为非空列表 $T_{\text{src}} = \{ \texttt{type}: \texttt{array}, \texttt{items}: T \}$（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）；
  * `index`（必填）：必须为非空整数（若为引用表达式须满足 $\text{TypeOf}(\Sigma, \text{index}) <: \{ \texttt{type}: \texttt{integer} \}$；若为字面量须为整数字面量 $idx \in \mathbb{Z}$，支持负数表示从列表末尾倒数索引）；
  * `default`（可选）：若声明，当为变量或路径引用 $E_{\text{def}}$ 时，须满足 $\text{TypeOf}(\Sigma, E_{\text{def}}) <: \Sigma(v_{\text{out}})$；当为常量字面量 $L_{\text{def}}$ 时，须满足 $L_{\text{def}} \models \Sigma(v_{\text{out}})$。
* **出参校验与静态约束**：
  * `value`（必填，绑定顶层变量 $v_{\text{out}}$）：要求 $v_{\text{out}} \in \text{dom}(\Sigma)$ 且满足子类型兼容 $T <: \Sigma(v_{\text{out}})$；
  * **未声明 `default` 时的根变量异名约束**：$v_{\text{out}} \neq \text{RootVar}(\text{with.input.source})$，且当 `index` 为引用表达式时 $v_{\text{out}} \neq \text{RootVar}(\text{with.input.index})$。
* **静态控制分支与反定值（Kill）契约推导**：
  * 未声明 `default` 时：暴露 `.some`（下标未越界，$\text{OutVars} = \{v_{\text{out}}\}, \; \text{KillVars} = \emptyset$）与 `.none`（下标越界，$\text{OutVars} = \emptyset, \; \text{KillVars} = \{v_{\text{out}}\}$，详见 [`03-branch-sensitive-definite-assignment.md`](../03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)）；
  * 声明了 `default` 时：无分支单出边（$\text{OutVars} = \{v_{\text{out}}\}$）。

### 9. `builtin:array_shift`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须为非空列表 $\{ \texttt{type}: \texttt{array}, \texttt{items}: T \}$（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）。
* **出参校验与静态约束**：
  * `item`（必填，绑定顶层变量 $v_{\text{item}}$）：要求 $v_{\text{item}} \in \text{dom}(\Sigma)$ 且满足子类型兼容 $T <: \Sigma(v_{\text{item}})$；
  * `rest`（必填，绑定顶层变量 $v_{\text{rest}}$）：要求 $v_{\text{rest}} \in \text{dom}(\Sigma)$ 且满足子类型兼容 $T_{\text{src}} <: \Sigma(v_{\text{rest}})$（允许 $v_{\text{rest}} = \text{RootVar}(\text{source})$）；
  * **出参异名约束**：$v_{\text{item}} \neq v_{\text{rest}}$。
* **静态控制分支与反定值（Kill）契约推导**：暴露 `.some`（$\text{OutVars} = \{v_{\text{item}}, v_{\text{rest}}\}, \; \text{KillVars} = \emptyset$）与 `.empty`（$\text{OutVars} = \emptyset, \; \text{KillVars} = \{v_{\text{item}}\}$，详见 [`03-branch-sensitive-definite-assignment.md`](../03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)）。

### 10. `builtin:array_append`
* **入参与出参校验**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：要求 $v_{\text{res}} \in \text{dom}(\Sigma)$，且目标类型 $T_{\text{res}} = \Sigma(v_{\text{res}}) = \{ \texttt{type}: \texttt{array}, \texttt{items}: T_{\text{target}} \}$ 必须为非空列表（$\text{Nullable}(T_{\text{res}}) = \texttt{false}$），无分支单出边（$\text{OutVars} = \{v_{\text{res}}\}$）；
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$，必须为非空列表（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）且满足子类型兼容 $T_{\text{src}} <: T_{\text{res}}$（允许 $v_{\text{res}} = \text{RootVar}(\text{source})$）；
  * `item`（必填）：若为引用表达式须满足 $\text{TypeOf}(\Sigma, \text{item}) <: T_{\text{target}}$；若为字面量须满足 $L \models T_{\text{target}}$。

### 11. `builtin:array_concat`
* **入参与出参校验**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：要求 $v_{\text{res}} \in \text{dom}(\Sigma)$，且目标类型 $T_{\text{res}} = \Sigma(v_{\text{res}}) = \{ \texttt{type}: \texttt{array}, \texttt{items}: T_{\text{target}} \}$ 必须为非空列表（$\text{Nullable}(T_{\text{res}}) = \texttt{false}$）；
  * `left`、`right`（必填）：设 $T_L = \text{TypeOf}(\Sigma, \text{with.input.left})$，$T_R = \text{TypeOf}(\Sigma, \text{with.input.right})$（或由 `{ value: [...] }` 数组字面量校验兼容），必须均为非空列表（$\text{Nullable}(T_L) = \text{Nullable}(T_R) = \texttt{false}$），且同时满足对目标列表类型的协变子类型兼容：
    $$T_L <: T_{\text{res}} \;\land\; T_R <: T_{\text{res}}$$
    *(允许 $v_{\text{res}}$ 与 `left` 或 `right` 的根变量同名。)*
* **静态控制分支推导**：
  * 无分支单出边（$\text{OutVars} = \{v_{\text{res}}\}$）。

### 12. `builtin:array_flatten`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须满足外层与内层均为非空列表：
    $$\text{Nullable}(T_{\text{src}}) = \texttt{false} \;\land\; \text{BaseType}(T_{\text{src}}) = \texttt{array} \;\land\; \text{Nullable}(T_{\text{src}}.\text{items}) = \texttt{false} \;\land\; \text{BaseType}(T_{\text{src}}.\text{items}) = \texttt{array}$$
    否则静态检查器在编译期报错。令内层元素类型 $W = T_{\text{src}}.\text{items}.\text{items}$。
* **出参推导 / 校验**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：一维列表类型 $T_{\text{res}} = \{ \texttt{type}: \texttt{array}, \texttt{items}: W \}$，要求 $v_{\text{res}} \in \text{dom}(\Sigma)$ 且满足子类型兼容 $T_{\text{res}} <: \Sigma(v_{\text{res}})$。
* **静态控制分支推导**：
  * 无分支单出边（$\text{OutVars} = \{v_{\text{res}}\}$）。

---

## 四、 有序映射与强类型结构体算子

### 13. `builtin:map_get`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须为非空有序映射 $T_{\text{src}} = \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: T_V \}$（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）；
  * `key`（必填）：若为引用表达式须满足 $\text{TypeOf}(\Sigma, \text{key}) <: \{ \texttt{type}: \texttt{string} \}$；若为字面量须为 `{ value: "<str>" }`；
  * `default`（可选）：若声明，当为变量或路径引用 $E_{\text{def}}$ 时，须满足 $\text{TypeOf}(\Sigma, E_{\text{def}}) <: \Sigma(v_{\text{out}})$；当为常量字面量 $L_{\text{def}}$ 时，须满足 $L_{\text{def}} \models \Sigma(v_{\text{out}})$。
* **出参校验与静态约束**：
  * `value`（必填，绑定顶层变量 $v_{\text{out}}$）：要求 $v_{\text{out}} \in \text{dom}(\Sigma)$ 且满足子类型兼容 $T_V <: \Sigma(v_{\text{out}})$；
  * **根变量异名约束**：$v_{\text{out}} \neq \text{RootVar}(\text{with.input.source})$。
* **静态控制分支与反定值（Kill）契约推导**：
  * 未声明 `default` 时：暴露 `.some`（$\text{OutVars} = \{v_{\text{out}}\}, \; \text{KillVars} = \emptyset$）与 `.none`（$\text{OutVars} = \emptyset, \; \text{KillVars} = \{v_{\text{out}}\}$，详见 [`03-branch-sensitive-definite-assignment.md`](../03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)）；
  * 声明了 `default` 时：无分支单出边（$\text{OutVars} = \{v_{\text{out}}\}$）。

### 14. `builtin:map_set`
* **入参与出参校验**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：要求 $v_{\text{res}} \in \text{dom}(\Sigma)$，且目标类型 $T_{\text{res}} = \Sigma(v_{\text{res}}) = \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: T_{V,\text{target}} \}$ 必须为非空有序映射（$\text{Nullable}(T_{\text{res}}) = \texttt{false}$），无分支单出边（$\text{OutVars} = \{v_{\text{res}}\}$）；
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$，必须为非空有序映射（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）且满足子类型兼容 $T_{\text{src}} <: T_{\text{res}}$（允许 $v_{\text{res}} = \text{RootVar}(\text{source})$）；
  * `key`（必填）：若为引用表达式须满足 $\text{TypeOf}(\Sigma, \text{key}) <: \{ \texttt{type}: \texttt{string} \}$；若为字面量须为 `{ value: "<str>" }`；
  * `value`（必填）：若为引用表达式须满足 $\text{TypeOf}(\Sigma, \text{value}) <: T_{V,\text{target}}$；若为字面量须满足 $L \models T_{V,\text{target}}$。

### 15. `builtin:map_delete`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须为非空有序映射 $T_{\text{src}} = \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: T_V \}$（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）；
  * `key`（必填）：若为引用表达式须满足 $\text{TypeOf}(\Sigma, \text{key}) <: \{ \texttt{type}: \texttt{string} \}$；若为字面量须为 `{ value: "<str>" }`。
* **出参校验**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：要求 $v_{\text{res}} \in \text{dom}(\Sigma)$ 且满足子类型兼容 $T_{\text{src}} <: \Sigma(v_{\text{res}})$（允许 $v_{\text{res}} = \text{RootVar}(\text{source})$），无分支单出边（$\text{OutVars} = \{v_{\text{res}}\}$）。

### 16. `builtin:object_merge`
* **入参与出参类型校验**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：要求 $v_{\text{res}} \in \text{dom}(\Sigma)$，且目标类型 $T_{\text{target}} = \Sigma(v_{\text{res}})$ 必须为非空强类型结构体（`TypedDict`：$\text{BaseType}(T_{\text{target}}) = \texttt{object} \land \text{Nullable}(T_{\text{target}}) = \texttt{false} \land T_{\text{target}}.\text{properties} \neq \emptyset$）。
  * 除可选保留入参键 `base` 外，`with.input` 中的其余每个键 $f_i \in \{f_1, \dots, f_m\}$（要求 $m \ge 1$，且每个 $f_i \neq \texttt{"base"}$ 为合法属性名）代表目标结构体 $T_{\text{target}}$ 的顶层属性名，且必须满足 $\{f_1, \dots, f_m\} \subseteq \text{dom}(T_{\text{target}}.\text{properties})$（禁止向闭合 `TypedDict` 写入未声明的属性）。
  * **覆盖或构造字段兼容校验**：对每个字段 $f_i \in \{f_1, \dots, f_m\}$（记目标属性声明类型为 $T_{\text{prop}, i} = T_{\text{target}}.\text{properties}[f_i]$）：
    * 若 $f_i$ 绑定引用表达式 $E_i$：统一校验子类型兼容 $\text{TypeOf}(\Sigma, E_i) <: T_{\text{prop}, i}$（由于子类型公理 1 已涵盖非空向可空提升，非空表达式天然兼容可空目标属性）；
    * 若 $f_i$ 绑定常量字面量 $L_i$：校验实例兼容性 $L_i \models T_{\text{prop}, i}$（或当 $f_i \notin \text{set}(T_{\text{target}}.\text{required})$ 且 $\text{Nullable}(T_{\text{prop}, i}) = \texttt{true}$ 时允许 $L_i = \texttt{null}$）。
  * **情况 A（声明了 `base`）**：
    设 $T_{\text{base}} = \text{TypeOf}(\Sigma, \text{with.input.base})$，必须为非空 `TypedDict`（允许 $v_{\text{res}} = \text{RootVar}(\text{base})$），且满足：
    1. 属性域在目标结构体声明范围内且与覆盖字段合并后覆盖目标全部必填项：
       $$\text{dom}(T_{\text{base}}.\text{properties}) \subseteq \text{dom}(T_{\text{target}}.\text{properties}) \;\land\; \text{set}(T_{\text{target}}.\text{required}) \subseteq \text{set}(T_{\text{base}}.\text{required}) \cup \{f_1, \dots, f_m\}$$
    2. 对所有未被本次覆盖的保留字段 $f \in \text{dom}(T_{\text{base}}.\text{properties}) \setminus \{f_1, \dots, f_m\}$，满足子类型兼容：
       $$T_{\text{base}}.\text{properties}[f] <: T_{\text{target}}.\text{properties}[f]$$
  * **情况 B（未声明 `base`，直接从字段构造 `TypedDict`）**：
    提供的字段集合必须覆盖 $T_{\text{target}}$ 的全部必填属性且不超出声明属性集：
    $$\text{set}(T_{\text{target}}.\text{required}) \subseteq \{f_1, \dots, f_m\} \subseteq \text{dom}(T_{\text{target}}.\text{properties})$$
* **静态控制分支推导**：
  * 无分支单出边（$\text{OutVars} = \{v_{\text{res}}\}$）。
