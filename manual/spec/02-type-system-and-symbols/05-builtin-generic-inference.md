# 05. 内建算子（`builtin:*`）编译期泛型类型推导规则

所有 `use: builtin:<op>` 节点在编译期执行静态泛型类型推导（Generic Type Inference）。令当前图的符号表为 $\Sigma$，定义算子 $\text{StripNull}(T)$ 为将显式可空类型 $T = [\text{BaseType}, \texttt{"null"}]$ 移除 $\texttt{"null"}$ 并保留其余全部约束字段后的非空 `TypeSpec`。

---

## 一、 各内建算子的形式化类型推导公理

### 1. `builtin:unwrap`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须满足 $\text{Nullable}(T_{\text{src}}) = \texttt{true}$，否则报编译期错误（禁止对非空类型冗余拆包）。令非空内层类型 $T = \text{StripNull}(T_{\text{src}})$。
  * `default`（可选）：若声明，当为变量或路径引用 $E_{\text{def}}$ 时，须满足 $\text{TypeOf}(\Sigma, E_{\text{def}}) \equiv T$；当为常量字面量 $L_{\\text{def}}$ 时，须满足 $L_{\text{def}} \neq \texttt{null}$ 且 $L_{\text{def}} \models T$。
* **出参推导与静态约束**：
  * `value`（必填，绑定顶层变量 $v_{\text{out}}$）：推导类型 $\Sigma(v_{\text{out}}) = T$。
  * **根变量异名约束**：$v_{\text{out}} \neq \text{RootVar}(\text{with.input.source})$（因源变量可空而输出变量非空，同一栈帧内不可同名不同类型）。
* **静态控制分支与反定值（Kill）契约推导**：
  * 未声明 `default` 时：暴露 `.some`（`output_subset = {v_out}`，$\text{KillVars} = \emptyset$）与 `.none`（`output_subset = \emptyset$，$\text{KillVars} = \{v_{\text{out}}\}$，详见 [`03-branch-sensitive-definite-assignment.md`](../03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)）；
  * 声明了 `default` 时：无分支单出边（`output_subset = {v_out}`）。

### 2. `builtin:switch`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须满足 $\text{Nullable}(T_{\text{src}}) = \texttt{false}$，且属于以下二者之一：
    1. $\text{BaseType}(T_{\text{src}}) = \texttt{boolean}$；
    2. $\text{BaseType}(T_{\text{src}}) = \texttt{string}$ 且声明了非空 `enum: [e_1, ..., e_m]`，同时每个枚举元素必须为合法的分支标识符（`BranchId`）：
       $$\forall e_i \in T_{\text{src}}.\text{enum}, \quad e_i \text{ 匹配 } \texttt{^[a-zA-Z\_][a-zA-Z0-9\_]*\$} \;\land\; e_i \notin \{\texttt{"start"}, \texttt{"end"}, \texttt{"fail"}\}$$
       若存在任何枚举值不是合法的 `BranchId`，静态检查器在编译期报错。
* **出参约束**：禁止声明 `with.output`。
* **静态控制分支推导**：
  * 当为 `boolean` 时：暴露 `.true` 与 `.false`（`output_subset = \emptyset`）；
  * 当为 `string` + `enum` 时：暴露 `.e_1`, ..., `.e_m`（`output_subset = \emptyset`）。

### 3. `builtin:assign`
* **入参校验与出参类型推导**：
  * `source`（必填）：支持绑定顶层变量、`FieldPathExpr` 静态字段路径或常量字面量（含数值/布尔/`null` 标量字面量与 `{ value: <literal> }` 包装字面量）。
  * `value`（必填，绑定顶层变量 $v_{\text{out}}$）：
    1. **当 `with.input.source` 为变量或静态字段路径引用 $E$ 时**：
       令 $T_{\text{src}} = \text{TypeOf}(\Sigma, E)$，推导输出变量类型 $\Sigma(v_{\text{out}}) = T_{\text{src}}$（若 $v_{\text{out}} \in \text{dom}(\Sigma)$，须满足静态类型等价 $\Sigma(v_{\text{out}}) \equiv T_{\text{src}}$）。
    2. **当 `with.input.source` 为常量字面量 $L$ 时**：
       * 若 $v_{\text{out}} \in \text{dom}(\Sigma)$（已在显式契约或前序推导中获得目标类型 $T = \Sigma(v_{\text{out}})$）：校验实例兼容性 $L \models T$，输出类型保持 $\Sigma(v_{\text{out}}) = T$；
       * 若 $v_{\text{out}} \notin \text{dom}(\Sigma)$：要求 $L$ 必须为非空基础标量字面量（`string` / `integer` / `float` / `boolean`），将其推导为对应的无 `enum` 非空基础标量 `TypeSpec` $T_{\text{scalar}}$ 并注册 $\Sigma(v_{\text{out}}) = T_{\text{scalar}}$；若 $L$ 为 `null`、数组 `[]` 或对象 `{}` 字面量且 $v_{\text{out}} \notin \text{dom}(\Sigma)$，编译期报错。
* **静态控制分支推导**：
  * 无分支单出边（`output_subset = {v_out}`）。

### 4. `builtin:array_shift`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须为非空列表 `{ type: array, items: T }`（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）。
* **出参推导与静态约束**：
  * `item`（必填，绑定顶层变量 $v_{\text{item}}$）：推导类型 $\Sigma(v_{\text{item}}) = T$；
  * `rest`（必填，绑定顶层变量 $v_{\text{rest}}$）：推导类型 $\Sigma(v_{\text{rest}}) = \{ \texttt{type}: \texttt{array}, \texttt{items}: T \}$（允许 $v_{\text{rest}} = \text{RootVar}(\text{source})$）；
  * **出参异名约束**：$v_{\text{item}} \neq v_{\text{rest}}$。
* **静态控制分支与反定值（Kill）契约推导**：暴露 `.some`（`output_subset = {v_item, v_rest}`，$\text{KillVars} = \emptyset$）与 `.empty`（`output_subset = \emptyset$，$\text{KillVars} = \{v_{\text{item}}\}$，详见 [`03-branch-sensitive-definite-assignment.md`](../03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)）。

### 5. `builtin:array_append`
* **入参校验**：
  * `source`（必填）：必须为非空列表 $T_{\text{src}} = \{ \texttt{type}: \texttt{array}, \texttt{items}: T \}$（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）；
  * `item`（必填）：若为引用表达式须满足 $\text{TypeOf}(\Sigma, \text{item}) \equiv T$；若为字面量须满足 $L \models T$。
* **出参推导**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：推导类型 $\Sigma(v_{\text{res}}) = T_{\text{src}}$，无分支单出边（`output_subset = {v_res}`）。

### 6. `builtin:map_set`
* **入参校验**：
  * `source`（必填）：必须为非空有序映射 $T_{\text{src}} = \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: T_V \}$（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）；
  * `key`（必填）：必须为非空 `type: string`（引用表达式或 `{ value: "<str>" }` 字面量）；
  * `value`（必填）：若为引用表达式须满足 $\text{TypeOf}(\Sigma, \text{value}) \equiv T_V$；若为字面量须满足 $L \models T_V$。
* **出参推导**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：推导类型 $\Sigma(v_{\text{res}}) = T_{\text{src}}$，无分支单出边（`output_subset = {v_res}`）。

### 7. `builtin:map_get`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须为非空有序映射 $T_{\text{src}} = \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: T_V \}$（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）；
  * `key`（必填）：必须为非空 `type: string`（引用表达式或 `{ value: "<str>" }` 字面量）；
  * `default`（可选）：若声明，当为变量或路径引用 $E_{\text{def}}$ 时，须满足 $\text{TypeOf}(\Sigma, E_{\text{def}}) \equiv T_V$；当为常量字面量 $L_{\text{def}}$ 时，须满足 $L_{\text{def}} \models T_V$。
* **出参推导与静态约束**：
  * `value`（必填，绑定顶层变量 $v_{\text{out}}$）：推导类型 $\Sigma(v_{\text{out}}) = T_V$；
  * **根变量异名约束**：$v_{\text{out}} \neq \text{RootVar}(\text{with.input.source})$。
* **静态控制分支与反定值（Kill）契约推导**：
  * 未声明 `default` 时：暴露 `.some`（`output_subset = {v_out}`，$\text{KillVars} = \emptyset$）与 `.none`（`output_subset = \emptyset$，$\text{KillVars} = \{v_{\text{out}}\}$，详见 [`03-branch-sensitive-definite-assignment.md`](../03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)）；
  * 声明了 `default` 时：无分支单出边（`output_subset = {v_out}`）。

### 8. `builtin:map_delete`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须为非空有序映射 $T_{\text{src}} = \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: T_V \}$（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）；
  * `key`（必填）：必须为非空 `type: string`（引用表达式或 `{ value: "<str>" }` 字面量）。
* **出参推导**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：推导类型 $\Sigma(v_{\text{res}}) = T_{\text{src}}$（允许 $v_{\text{res}} = \text{RootVar}(\text{source})$），无分支单出边（`output_subset = {v_res}`）。

### 9. `builtin:object_merge`
* **入参校验与出参类型推导**：
  * 除可选保留入参键 `base` 外，`with.input` 中的其余每个键 $f_i \in \{f_1, \dots, f_m\}$（要求 $m \ge 1$，且每个 $f_i \neq \texttt{"base"}$ 为合法属性名）代表目标结构体（`TypedDict`）的顶层属性名。
  * **情况 A（声明了 `base`）**：
    1. 设 $T_{\text{base}} = \text{TypeOf}(\Sigma, \text{with.input.base})$。必须满足 $T_{\text{base}}$ 为非空强类型结构体（`TypedDict`：$\text{BaseType}(T_{\text{base}}) = \texttt{object} \land \text{Nullable}(T_{\text{base}}) = \texttt{false} \land T_{\text{base}}.\text{properties} \neq \emptyset$）；
    2. 对每个覆盖字段 $f_i \in \{f_1, \dots, f_m\}$，必须满足 $f_i \in \text{dom}(T_{\text{base}}.\text{properties})$（禁止向闭合 `TypedDict` 写入未声明的属性），记属性声明类型为 $T_{\text{prop}, i} = T_{\text{base}}.\text{properties}[f_i]$：
       * 若 $f_i$ 绑定引用表达式 $E_i$（记 $T_i = \text{TypeOf}(\Sigma, E_i)$）：当 $f_i \in \text{set}(T_{\text{base}}.\text{required})$ 时须满足 $T_i \equiv T_{\text{prop}, i}$；当 $f_i \notin \text{set}(T_{\text{base}}.\text{required})$ 时允许 $T_i \equiv T_{\text{prop}, i}$ 或 $T_i \equiv \text{MakeNullable}(T_{\text{prop}, i})$；
       * 若 $f_i$ 绑定常量字面量 $L_i$：须满足 $L_i \models T_{\text{prop}, i}$（或当 $f_i \notin \text{set}(T_{\text{base}}.\text{required})$ 时允许 $L_i = \texttt{null}$）；
    3. `result`（必填，绑定顶层变量 $v_{\text{res}}$）：推导类型 $\Sigma(v_{\text{res}}) = T_{\text{base}}$（允许 $v_{\text{res}} = \text{RootVar}(\text{base})$）。
  * **情况 B（未声明 `base`，直接从字段构造 `TypedDict`）**：
    * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：
      1. **若 $v_{\text{res}} \in \text{dom}(\Sigma)$（已在显式契约或前序推导中获得目标类型 $T_{\text{target}} = \Sigma(v_{\text{res}})$）**：
         * $T_{\text{target}}$ 必须为非空 `TypedDict`；
         * 字段集合必须覆盖全部必填属性且不超出声明属性集：
           $$\text{set}(T_{\text{target}}.\text{required}) \subseteq \{f_1, \dots, f_m\} \subseteq \text{dom}(T_{\text{target}}.\text{properties})$$
         * 每个字段 $f_i$ 的绑定值须按情况 A 第 2 步相同规则与 $T_{\text{target}}.\text{properties}[f_i]$ 兼容；输出类型保持 $\Sigma(v_{\text{res}}) = T_{\text{target}}$。
      2. **若 $v_{\text{res}} \notin \text{dom}(\Sigma)$（由字段直接正向推导闭合 `TypedDict`）**：
         * 对每个字段 $f_i \in \{f_1, \dots, f_m\}$：若绑定引用表达式 $E_i$，令 $T_{f_i} = \text{TypeOf}(\Sigma, E_i)$；若绑定常量字面量 $L_i$，要求 $L_i$ 为非空基础标量字面量并推导为对应的无 `enum` 非空基础标量类型 $T_{f_i}$；
         * 推导构造闭合非空 `TypedDict`（全部字段列入 `required`）：
           $$\Sigma(v_{\text{res}}) = \{ \texttt{type}: \texttt{object}, \texttt{properties}: \{ f_1: T_{f_1}, \dots, f_m: T_{f_m} \}, \texttt{required}: [f_1, \dots, f_m] \}$$
* **静态控制分支推导**：
  * 无分支单出边（`output_subset = {v_res}`）。
