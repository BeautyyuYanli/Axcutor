# 04. 静态字段路径投影类型推导（Static Field Projection Typing）

本规范定义只读静态字段路径表达式（`FieldPathExpr`）$E = v_0 . f_1 . f_2 \dots f_k$（$k \ge 0$，其中 $k = 0$ 表示顶层变量引用 $E = v_0$，$k \ge 1$ 表示嵌套字段投影）在符号表 $\Sigma$ 下的编译期类型推导算法 $\text{TypeOf}(\Sigma, E)$ 及运行期求值方程 $\text{Eval}(\rho, E)$。

定义可空提升算子 $\text{MakeNullable}(T)$：
* 若 $\text{Nullable}(T) = \texttt{true}$，则 $\text{MakeNullable}(T) = T$；
* 若 $\text{Nullable}(T) = \texttt{false}$（即 $T.\text{type} = b \in \mathcal{B}$），则 $\text{MakeNullable}(T)$ 为将 $T.\text{type}$ 提升为 $[b, \texttt{"null"}]$ 并保留其余全部约束字段后的显式可空 `TypeSpec`。

---

## 一、 编译期归纳类型推导规则（$\text{TypeOf}(\Sigma, E)$）

给定当前作用域符号表 $\Sigma$ 与字段路径表达式 $E = v_0 . f_1 . f_2 \dots f_k$（$k \ge 0$），按以下归纳步骤推导其静态类型：

1. **基础情形（$k = 0$）与根变量绑定**：
   根变量 $v_0 = \text{RootVar}(E)$ 必须存在于当前符号表 $\Sigma$ 中，并由此确立初始类型 $T_0$：
   $$v_0 \in \text{dom}(\Sigma), \quad T_0 = \Sigma(v_0)$$
   当 $k = 0$（即顶层变量引用 $E = v_0$）时，推导直接终止，$\text{TypeOf}(\Sigma, v_0) = T_0 = \Sigma(v_0)$。

2. **归纳情形（$k \ge 1$）：逐级非空结构体穿透校验**：
   当 $k \ge 1$ 时，对每一步 $i = 1, 2, \dots, k$，基于上一级类型 $T_{i-1}$ 依次执行以下校验与类型递推：
   * **前缀非空性约束**：前缀类型 $T_{i-1}$ 必须满足 $\text{Nullable}(T_{i-1}) = \texttt{false}$。禁止对显式可空结构体（`type: [object, "null"]`）直接执行 `.field` 投影；必须先通过 `builtin:unwrap` 收窄为非空结构体变量。
   * **`TypedDict` 形态与属性存在性约束**：$T_{i-1}$ 的基础类型必须为 `object` 且包含 `properties`（即 $T_{i-1}$ 为强类型结构体 `TypedDict`，禁止对 `OrderedMap`、`array` 或基础标量执行 `.field` 投影），且当前字段名 $f_i$ 必须存在于属性映射中：
     $$f_i \in \text{dom}(T_{i-1}.\text{properties})$$
   * **中间穿透步（$1 \le i < k$）的必填性约束**：当 $i < k$（后继仍需继续向下穿透 `.f_{i+1}`）时，字段 $f_i$ 必须为必填属性，即 $f_i \in \text{set}(T_{i-1}.\text{required})$，此时令 $T_i = T_{i-1}.\text{properties}[f_i]$。若 $f_i \notin \text{set}(T_{i-1}.\text{required})$，直接报编译期错误——禁止对可能缺失的非必填结构体属性直接穿透，必须先将该可选结构体属性通过 `builtin:unwrap` 拆包为非空顶层变量后再访问其内部字段。
   * **最末级叶子步（$i = k$）的类型合成**：在路径最末级字段 $f_k$ 处，根据其是否必填合成叶子类型 $T_k$：
     $$T_k = \begin{cases} T_{k-1}.\text{properties}[f_k], & \text{若 } f_k \in \text{set}(T_{k-1}.\text{required}) \\ \text{MakeNullable}(T_{k-1}.\text{properties}[f_k]), & \text{若 } f_k \notin \text{set}(T_{k-1}.\text{required}) \end{cases}$$

3. **最终静态类型确立**：
   对任意 $k \ge 0$，路径表达式 $E$ 的静态类型确立为：
   $$\text{TypeOf}(\Sigma, E) = T_k$$
   当 $k \ge 1$ 且叶子属性 $f_k \notin \text{set}(T_{k-1}.\text{required})$ 时，$T_k = \text{MakeNullable}(T_{k-1}.\text{properties}[f_k])$ 满足 $\text{Nullable}(T_k) = \texttt{true}$；类型检查器既阻止将其直接赋给要求非空类型的目标参数，又允许将其传入 `builtin:unwrap`（如 `with: { input: { source: user.nickname } }`）安全拆包为非空值。

---

## 二、 运行期求值契约（$\text{Eval}(\rho, E)$）

给定运行期变量环境 $\rho$（满足 $\forall v \in \text{dom}(\Sigma),\; \rho(v) \models \Sigma(v)$）与通过上述编译期类型检查的字段路径表达式 $E = v_0 . f_1 . f_2 \dots f_k$（$k \ge 0$），运行期求值方程 $\text{Eval}(\rho, E)$ 归纳定义如下：

1. **基础情形（$k = 0$，顶层变量求值）**：
   $$\text{Eval}(\rho, v_0) = \rho(v_0)$$

2. **归纳情形（$k \ge 1$，逐级字段投影求值）**：
   记前缀路径的运行期求值结果为 $u_{i-1} = \text{Eval}(\rho, v_0 . f_1 \dots f_{i-1})$（其中 $u_0 = \rho(v_0)$）。由编译期非空性约束与中间步必填性约束保证，对一切 $1 \le i < k$，父对象 $u_{i-1}$ 均为非 `null` 字典实例且必含键 $f_i$，故中间步满足 $u_i = u_{i-1}[f_i]$。在最末级叶子字段 $f_k$ 处，若父对象实例 $u_{k-1}$ 中不存在键 $f_k$，统一求值为 `null`：
   $$\text{Eval}(\rho, v_0 . f_1 \dots f_k) = \begin{cases} u_{k-1}[f_k], & \text{若 } f_k \in \text{dom}(u_{k-1}) \\ \texttt{null}, & \text{若 } f_k \notin \text{dom}(u_{k-1}) \end{cases}$$
