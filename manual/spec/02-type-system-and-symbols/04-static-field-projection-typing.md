# 04. 静态字段路径投影类型推导（Static Field Projection Typing）

本规范定义只读静态字段路径表达式 $E = v_0 . f_1 . f_2 \dots f_k$（$k \ge 1$）在符号表 $\Sigma$ 下的编译期类型推导算法 $\text{TypeOf}(\Sigma, E)$。

---

## 一、 归纳类型推导规则

给定当前作用域符号表 $\Sigma$ 与字段路径表达式 $E = v_0 . f_1 . f_2 \dots f_k$（$k \ge 1$）：

1. **根变量存在性检查**：
   * 根变量 $v_0 = \text{RootVar}(E)$ 必须存在于当前符号表 $\Sigma$ 中（$v_0 \in \text{dom}(\Sigma)$），记初始类型 $T_0 = \Sigma(v_0)$。
2. **逐级非空结构体穿透校验（Step-by-Step Induction）**：
   * 对于每一步 $i = 1, 2, \dots, k$：
     1. **非空性检查**：前缀类型 $T_{i-1}$ 必须满足 $\text{Nullable}(T_{i-1}) = \texttt{false}$。
        *(禁止对显式可空结构体 `type: [object, "null"]` 直接执行 `.field` 投影；必须先通过 `builtin:unwrap` 收窄为非空结构体变量。)*
     2. **`TypedDict` 形态检查**：$T_{i-1}$ 的基础类型必须为 `object` 且包含 `properties`（即 $T_{i-1}$ 为强类型结构体 `TypedDict`；禁止对 `OrderedMap`、`array` 或基础标量执行 `.field` 投影）。
     3. **字段存在性与必填保证**：字段名 $f_i$ 必须满足 $f_i \in \text{dom}(T_{i-1}.\text{properties})$ 且 $f_i \in \text{set}(T_{i-1}.\text{required})$。
     4. **递推下一级类型**：令 $T_i = T_{i-1}.\text{properties}[f_i]$。
3. **最终推导结果**：
   * 路径表达式 $E$ 的静态类型确立为最末级字段的类型 $\text{TypeOf}(\Sigma, E) = T_k$（最末级字段 $T_k$ 本身允许是任意良构的 `TypeSpec`，包括可空类型 `[BaseType, "null"]`、列表、结构体或标量）。
