# 05. 内建算子（`builtin:*`）编译期泛型类型推导规则

所有 `use: builtin:<op>` 节点在编译期执行静态泛型类型推导（Generic Type Inference）。令当前图的符号表为 $\Sigma$，定义算子 $\text{StripNull}(T)$ 为将显式可空类型 $T = [\text{BaseType}, \texttt{"null"}]$ 移除 $\texttt{"null"}$ 并保留其余全部约束字段后的非空 `TypeSpec`。

---

## 一、 各内建算子的形式化类型推导公理

### 1. `builtin:unwrap`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须满足 $\text{Nullable}(T_{\text{src}}) = \texttt{true}$，否则报编译期错误（禁止对非空类型冗余拆包）。令非空内层类型 $T = \text{StripNull}(T_{\text{src}})$。
  * `default`（可选）：若声明，当为变量或路径引用 $E_{\text{def}}$ 时，须满足 $\text{TypeOf}(\Sigma, E_{\text{def}}) \equiv T$；当为常量字面量 $L_{\text{def}}$ 时，须满足 $L_{\text{def}} \neq \texttt{null}$ 且 $L_{\text{def}} \models T$。
* **出参推导与静态约束**：
  * `value`（必填，绑定顶层变量 $v_{\text{out}}$）：推导类型 $\Sigma(v_{\text{out}}) = T$。
  * **根变量异名约束**：$v_{\text{out}} \neq \text{RootVar}(\text{with.input.source})$（因源变量可空而输出变量非空，同一栈帧内不可同名不同类型）。
  * **无预设初值约束**：当未声明 `default` 时，禁止 $v_{\text{out}} \in \text{dom}(G.\text{variables})$。
* **静态控制分支推导**：
  * 未声明 `default` 时：暴露 `.some`（`output_subset = {v_out}`）与 `.none`（`output_subset = \emptyset`）；
  * 声明了 `default` 时：无分支单出边（`output_subset = {v_out}`）。

### 2. `builtin:switch`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须满足 $\text{Nullable}(T_{\text{src}}) = \texttt{false}$，且属于以下二者之一：
    1. $\text{BaseType}(T_{\text{src}}) = \texttt{boolean}$；
    2. $\text{BaseType}(T_{\text{src}}) = \texttt{string}$ 且声明了非空 `enum: [e_1, ..., e_m]`。
* **出参约束**：禁止声明 `with.output`。
* **静态控制分支推导**：
  * 当为 `boolean` 时：暴露 `.true` 与 `.false`（`output_subset = \emptyset`）；
  * 当为 `string` + `enum` 时：暴露 `.e_1`, ..., `.e_m`（`output_subset = \emptyset`）。

### 3. `builtin:array_shift`
* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须为非空列表 `{ type: array, items: T }`（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）。
* **出参推导与静态约束**：
  * `item`（必填，绑定顶层变量 $v_{\text{item}}$）：推导类型 $\Sigma(v_{\text{item}}) = T$；
  * `rest`（必填，绑定顶层变量 $v_{\text{rest}}$）：推导类型 $\Sigma(v_{\text{rest}}) = \{ \texttt{type}: \texttt{array}, \texttt{items}: T \}$（允许 $v_{\text{rest}} = \text{RootVar}(\text{source})$）；
  * **异名与无预设初值约束**：$v_{\text{item}} \neq v_{\text{rest}}$，且禁止 $v_{\text{item}} \in \text{dom}(G.\text{variables})$。
* **静态控制分支推导**：暴露 `.some`（`output_subset = {v_item, v_rest}`）与 `.empty`（`output_subset = \emptyset`）。

### 4. `builtin:array_append`
* **入参校验**：
  * `source`（必填）：必须为非空列表 $T_{\text{src}} = \{ \texttt{type}: \texttt{array}, \texttt{items}: T \}$；
  * `item`（必填）：若为引用表达式须满足 $\text{TypeOf}(\Sigma, \text{item}) \equiv T$；若为字面量须满足 $L \models T$。
* **出参推导**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：推导类型 $\Sigma(v_{\text{res}}) = T_{\text{src}}$，无分支单出边（`output_subset = {v_res}`）。

### 5. `builtin:map_set`
* **入参校验**：
  * `source`（必填）：必须为非空有序映射 $T_{\text{src}} = \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: T_V \}$；
  * `key`（必填）：必须为非空 `type: string`（引用表达式或 `{ value: "<str>" }` 字面量）；
  * `value`（必填）：若为引用表达式须满足 $\text{TypeOf}(\Sigma, \text{value}) \equiv T_V$；若为字面量须满足 $L \models T_V$。
* **出参推导**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：推导类型 $\Sigma(v_{\text{res}}) = T_{\text{src}}$，无分支单出边（`output_subset = {v_res}`）。
