# 04. 静态字段路径投影类型推导（Static Field Projection Typing）

本规范定义只读静态字段路径表达式 $E = v_0 . f_1 . f_2 \dots f_k$（$k \ge 1$）在符号表 $\Sigma$ 下的编译期类型推导算法 $\text{TypeOf}(\Sigma, E)$ 及运行期求值契约。

定义可空提升算子 $\text{MakeNullable}(T)$：
* 若 $\text{Nullable}(T) = \texttt{true}$，则 $\text{MakeNullable}(T) = T$；
* 若 $\text{Nullable}(T) = \texttt{false}$（即 $T.\text{type} = b \in \mathcal{B}$），则 $\text{MakeNullable}(T)$ 为将 $T.\text{type}$ 提升为 $[b, \texttt{"null"}]$ 并保留其余全部约束字段后的显式可空 `TypeSpec`。

---

## 一、 归纳类型推导规则

给定当前作用域符号表 $\Sigma$ 与字段路径表达式 $E = v_0 . f_1 . f_2 \dots f_k$（$k \ge 1$）：

1. **根变量存在性检查**：
   * 根变量 $v_0 = \text{RootVar}(E)$ 必须存在于当前符号表 $\Sigma$ 中（$v_0 \in \text{dom}(\Sigma)$），记初始类型 $T_0 = \Sigma(v_0)$。
2. **逐级非空结构体穿透校验（Step-by-Step Induction）**：
   * 对于每一步 $i = 1, 2, \dots, k$：
     1. **前缀非空性检查**：前缀类型 $T_{i-1}$ 必须满足 $\text{Nullable}(T_{i-1}) = \texttt{false}$。
        *(禁止对显式可空结构体 `type: [object, "null"]` 直接执行 `.field` 投影；必须先通过 `builtin:unwrap` 收窄为非空结构体变量。)*
     2. **`TypedDict` 形态与属性存在性检查**：$T_{i-1}$ 的基础类型必须为 `object` 且包含 `properties`（即 $T_{i-1}$ 为强类型结构体 `TypedDict`；禁止对 `OrderedMap`、`array` 或基础标量执行 `.field` 投影），且字段名 $f_i$ 必须满足：
        $$f_i \in \text{dom}(T_{i-1}.\text{properties})$$
     3. **区分中间穿透步（$i < k$）与叶子投影步（$i = k$）的必填性处理**：
        * **若 $f_i \in \text{set}(T_{i-1}.\text{required})$（必填属性）**：
          令 $T_i = T_{i-1}.\text{properties}[f_i]$；
        * **若 $f_i \notin \text{set}(T_{i-1}.\text{required})$（非必填可选属性）**：
          * **当 $i < k$（中间穿透步，试图继续向下穿透 `.f_{i+1}`）时**：直接报编译期错误（禁止对可能缺失的非必填结构体属性直接穿透，正如禁止对可空结构体穿透一样；必须先将该可选结构体属性通过 `builtin:unwrap` 拆包为非空顶层变量后再访问其内部字段）；
          * **当 $i = k$（作为路径的最末级叶子属性）时**：令最终推导类型为显式可空提升类型：
            $$T_k = \text{MakeNullable}(T_{k-1}.\text{properties}[f_k])$$
            并在运行期对路径 $E$ 取值时，若父对象实例中不存在键 $f_k$，统一求值为 `null`。
3. **最终推导结果**：
   * 路径表达式 $E$ 的静态类型确立为最末级字段的推导类型 $\text{TypeOf}(\Sigma, E) = T_k$。
   * 当叶子属性 $f_k \notin \text{set}(T_{k-1}.\text{required})$ 时，由于 $T_k = \text{MakeNullable}(T_{k-1}.\text{properties}[f_k])$ 满足 $\text{Nullable}(T_k) = \texttt{true}$，类型检查器既阻止将其直接赋给要求非空类型的目标参数，又允许将其传入 `builtin:unwrap`（如 `with: { input: { source: user.nickname } }`）安全拆包为非空值。
