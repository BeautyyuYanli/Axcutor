# 02. 静态类型等价、子类型偏序与字面量实例兼容性算法

本规范定义编译期判定两个 `TypeSpec` 静态结构化等价（$T_1 \equiv T_2$）与单向静态子类型偏序（$T_1 <: T_2$）的形式化算法，以及判定常量字面量 $L$ 兼容于目标类型 $T$（$L \models T$）的实例校验规则。

---

## 一、 静态类型结构化等价判定（$T_1 \equiv T_2$）

两个良构的 `TypeSpec` $T_1$ 与 $T_2$ 静态等价（记作 $T_1 \equiv T_2$），用于校验同一栈帧内多处显式声明同一变量名时的类型一致性（$\Sigma_G(v) \equiv T$），当且仅当递归满足以下全部条件：

1. **基础类型与可空性一致**：
   * $\text{BaseType}(T_1) = \text{BaseType}(T_2)$（注意：在双向等价判定中，$\texttt{integer} \not\equiv \texttt{number}$）；
   * $\text{Nullable}(T_1) = \text{Nullable}(T_2)$（其中 `type: [b, "null"]` 与 `type: ["null", b]` 的 $\text{Nullable}$ 均为 $\texttt{true}$，彼此等价，但与 $\text{Nullable} = \texttt{false}$ 的非空类型不等价）。
2. **标量枚举集合一致**：
   * 若 $\text{BaseType}(T_1) \in \{\texttt{string}, \texttt{integer}, \texttt{number}, \texttt{boolean}\}$，则两者要么均未声明 `enum`，要么 $\text{set}(T_1.\text{enum}) = \text{set}(T_2.\text{enum})$。
3. **列表元素类型递归等价**：
   * 若 $\text{BaseType}(T_1) = \texttt{array}$，则 $T_1.\text{items} \equiv T_2.\text{items}$。
4. **有序映射值类型递归等价**：
   * 若 $T_1, T_2$ 均为 `OrderedMap`（含 `additionalProperties`），则 $T_1.\text{additionalProperties} \equiv T_2.\text{additionalProperties}$。
5. **结构体字段结构化递归等价（Structural Equivalence）**：
   * 若 $T_1, T_2$ 均为 `TypedDict`（含 `properties`），则当且仅当：
     * 字段名集合相同：$\text{dom}(T_1.\text{properties}) = \text{dom}(T_2.\text{properties})$；
     * 规范化后的必填字段集合相同：$\text{set}(T_1.\text{required}) = \text{set}(T_2.\text{required})$；
     * 对每个字段 $f \in \text{dom}(T_1.\text{properties})$，均有 $T_1.\text{properties}[f] \equiv T_2.\text{properties}[f]$。

---

## 二、 静态子类型偏序判定（$T_1 <: T_2$）

在值流转与契约消费位置（包括 `use: workflow:<path>` 传参与返回值绑定、`builtin:*` 算子入参/出参校验及 `for_each` 一阶集合提升目标绑定），编译器采用**单向静态子类型偏序关系（$T_{\text{actual}} <: T_{\text{expected}}$）**。由于 Axcutor 栈帧与容器算子均遵循纯函数式写时复制（Copy-on-Write）不可变语义，不存在原地可变别名引用，`Array`、`OrderedMap` 与 `TypedDict` 均支持安全的结构化协变（Structural Covariance）。

设 $T_1, T_2$ 为两个良构的 `TypeSpec`。定义 $T_1$ 是 $T_2$ 的子类型（记作 $T_1 <: T_2$），当且仅当递归满足以下五条公理：

### 1. 公理 1：可空性单调提升（Nullability Monotonicity）
$$\text{Nullable}(T_1) \implies \text{Nullable}(T_2)$$
* 非空类型（$\text{Nullable}(T_1) = \texttt{false}$）可作为非空类型（$\text{Nullable}(T_2) = \texttt{false}$）或显式可空类型（$\text{Nullable}(T_2) = \texttt{true}$）的子类型（即 $T <: \text{MakeNullable}(T)$）；
* 显式可空类型（$\text{Nullable}(T_1) = \texttt{true}$）**只能**作为显式可空类型（$\text{Nullable}(T_2) = \texttt{true}$）的子类型，**严禁**直接赋值给非空目标类型（必须先经 `builtin:unwrap` 拆包）。

### 2. 公理 2：标量基础类型与枚举子集拓宽（Scalar & Enum Widening）
当 $T_1, T_2$ 为标量类型时，在满足公理 1 的基础上：
1. **基础类型兼容**：
   * 要么 $\text{BaseType}(T_1) = \text{BaseType}(T_2) \in \{\texttt{string}, \texttt{integer}, \texttt{number}, \texttt{boolean}\}$；
   * 要么 $\text{BaseType}(T_1) = \texttt{integer} \;\land\; \text{BaseType}(T_2) = \texttt{number}$（整数向浮点数/通用数值拓宽，$\mathbb{Z} \subset \mathbb{R}$）。
2. **枚举约束拓宽**：
   * 若 $T_2$ 未声明 `enum`：无论 $T_1$ 是否声明 `enum`，均满足兼容（允许带 `enum` 的精确标量传入无 `enum` 的通用标量）；
   * 若 $T_2$ 声明了 `enum`：则 $T_1$ **也必须声明 `enum`**，且满足枚举值集合包含关系：
     $$\text{set}(T_1.\text{enum}) \subseteq \text{set}(T_2.\text{enum})$$

### 3. 公理 3：强类型列表协变（`Array` Covariance）
当 $\text{BaseType}(T_1) = \text{BaseType}(T_2) = \texttt{array}$ 时，在满足公理 1 的基础上，元素类型递归满足协变：
$$T_1.\text{items} <: T_2.\text{items}$$

### 4. 公理 4：强类型有序映射协变（`OrderedMap` Covariance）
当 $T_1, T_2$ 均为 `OrderedMap`（`type: object` + `additionalProperties`）时，在满足公理 1 的基础上，值类型递归满足协变：
$$T_1.\text{additionalProperties} <: T_2.\text{additionalProperties}$$

### 5. 公理 5：闭合结构体属性协变与必填性强化（`TypedDict` Covariance）
当 $T_1, T_2$ 均为 `TypedDict`（`type: object` + `properties`）时，在满足公理 1 的基础上（注：因运行期 `TypedDict` 视为闭合结构体 `additionalProperties: false`，禁止携带未在目标 Schema `properties` 中声明的额外字段，故不支持丢弃多余字段的宽度子类型，但支持**字段深度协变**与**必填性强化**）：
1. **属性域在目标声明范围内且覆盖目标全部必填项**：
   $$\text{set}(T_2.\text{required}) \subseteq \text{set}(T_1.\text{required}) \;\land\; \text{dom}(T_1.\text{properties}) \subseteq \text{dom}(T_2.\text{properties})$$
   *(即：$T_2$ 要求必填的字段在 $T_1$ 中也必须必填；$T_1$ 允许将 $T_2$ 中的可选字段强化为必填字段，或完全省略 $T_2$ 中的非必填字段。)*
2. **共有属性类型递归协变**：
   * 对每个属性 $f \in \text{dom}(T_1.\text{properties})$，须满足：
     $$T_1.\text{properties}[f] <: T_2.\text{properties}[f]$$
     *(注：若 $f \notin \text{set}(T_2.\text{required})$ 且 $T_2.\text{properties}[f]$ 本身声明为非空类型，在 JSON Schema 中表示“键要么省略、要么存在且非 `null`”，故 $T_1.\text{properties}[f]$ 同样须满足 $T_1.\text{properties}[f] <: T_2.\text{properties}[f]$，不可将可空属性赋给非空目标属性。)*

> **反对称性定理（Antisymmetry）与实例保持性定理（Value Preservation）**：
> * **反对称性**：$T_1 \equiv T_2 \iff (T_1 <: T_2 \;\land\; T_2 <: T_1)$，即 $(\text{TypeSpec}, <:)$ 构成偏序集（Poset）。
> * **实例保持性**：$\forall v, \; (v \models T_{\text{actual}} \;\land\; T_{\text{actual}} <: T_{\text{expected}}) \implies v \models T_{\text{expected}}$。任何符合子类型 $T_{\text{actual}}$ 的运行期 JSON 实例，无需任何运行期格式转换，天然符合父类型 $T_{\text{expected}}$ 的 JSON Schema 契约。

---

## 三、 字面量 Schema 实例兼容性校验（$L \models T$）

当在 `graph.variables` 初值声明或 `use.with.input` 常量传参中向目标类型 $T$ 提供字面量 $L$ 时，执行实例兼容性判定 $L \models T$：

1. **空值 `null` 规则**：
   * 当 $L = \texttt{null}$ 时：$L \models T \iff \text{Nullable}(T) = \texttt{true}$。非空类型禁止接受 `null` 字面量。
2. **非空字面量规则（当 $L \neq \texttt{null}$ 时）**：
   * **标量匹配**：
     * $\text{BaseType}(T) = \texttt{string} \implies L$ 必须为字符串；
     * $\text{BaseType}(T) = \texttt{integer} \implies L$ 必须为整数（非布尔值）；
     * $\text{BaseType}(T) = \texttt{number} \implies L$ 允许为**整数字面量**（如 `0`）或**浮点数字面量**（如 `0.0`）；
     * $\text{BaseType}(T) = \texttt{boolean} \implies L$ 必须为布尔值 `true` 或 `false`；
     * 若 $T$ 声明了 `enum`，则还必须满足 $L \in T.\text{enum}$。
   * **列表字面量匹配（$\text{BaseType}(T) = \texttt{array}$）**：
     * $L$ 必须为 JSON 数组，且对每个元素 $e \in L$，递归满足 $e \models T.\text{items}$（空数组字面量 `[]` 对任意良构 `array` 类型均满足兼容性）。
   * **有序映射字面量匹配（$T$ 为 `OrderedMap`）**：
     * $L$ 必须为 JSON 对象，且对每个值 $v \in \text{values}(L)$，递归满足 $v \models T.\text{additionalProperties}$（空对象字面量 `{}` 对任意良构 `OrderedMap` 均满足兼容性）。
   * **结构体字面量匹配（$T$ 为 `TypedDict`）**：
     * $L$ 必须为 JSON 对象，$\text{set}(T.\text{required}) \subseteq \text{dom}(L) \subseteq \text{dom}(T.\text{properties})$，且对每个键 $k \in \text{dom}(L)$，递归满足 $L[k] \models T.\text{properties}[k]$。
