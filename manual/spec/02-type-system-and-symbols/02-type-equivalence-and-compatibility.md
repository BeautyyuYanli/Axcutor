# 02. 静态类型等价、子类型偏序与字面量实例兼容性算法

本规范基于 [`01-typespec-well-formedness.md`](./01-typespec-well-formedness.md) 中良构 `TypeSpec` 的公理化定义及类型观察与变换算子 $\text{BaseType}(T)$、$\text{Nullable}(T)$ 与 $\text{MakeNullable}(T)$，定义编译期判定两个 `TypeSpec` 静态结构化等价（$T_1 \equiv T_2$）与单向静态子类型偏序（$T_1 <: T_2$）的形式化算法，以及判定常量字面量 $L$ 兼容于目标类型 $T$（$L \models T$）的实例校验规则。

---

## 一、 静态类型结构化等价判定（$T_1 \equiv T_2$）

设 $T_1, T_2$ 为满足 [`01-typespec-well-formedness.md`](./01-typespec-well-formedness.md) 良构性公理的两个 `TypeSpec`，其基础类型算子 $\text{BaseType}(T)$、可空性谓词 $\text{Nullable}(T)$ 与可空性提升算子 $\text{MakeNullable}(T)$ 均按 [`01-typespec-well-formedness.md`](./01-typespec-well-formedness.md) 定义。$T_1$ 与 $T_2$ 静态结构化等价（记作 $T_1 \equiv T_2$），用于校验同一栈帧内多处显式声明同一变量名时的类型一致性（$\Sigma_G(v) \equiv T$），当且仅当递归满足以下全部条件（注：`description` 注释信息以及 `enum`、`properties`、`required` 的书写次序不影响结构化等价判定）：

1. **基础类型与可空性一致**：
   * $\text{BaseType}(T_1) = \text{BaseType}(T_2)$；
   * $\text{Nullable}(T_1) = \text{Nullable}(T_2)$。
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

> **公理说明（Remark：基础类型严格区分与可空联合顺序无关性）**：
> * **数值基础类型不对称**：在双向结构化等价判定中，$\text{BaseType}(T_1) = \text{BaseType}(T_2)$ 要求基础类型严格同一，故 $\texttt{integer} \not\equiv \texttt{number}$。
> * **可空修饰顺序无关与幂等性**：由 [`01-typespec-well-formedness.md`](./01-typespec-well-formedness.md) 中 $\text{Nullable}(T)$ 与 $\text{MakeNullable}(T)$ 的定义，`type: [b, "null"]` 与 `type: ["null", b]` 的 $\text{BaseType}$ 均为 $b$ 且 $\text{Nullable}$ 均为 $\texttt{true}$，故两者满足 $T_1 \equiv T_2$，且对任意良构类型 $T$ 有 $\text{MakeNullable}(\text{MakeNullable}(T)) \equiv \text{MakeNullable}(T)$；但 $\text{Nullable}(T) = \texttt{true}$ 的可空类型与 $\text{Nullable}(T) = \texttt{false}$ 的非空类型恒不等价（$\text{MakeNullable}(T_{\text{non-null}}) \not\equiv T_{\text{non-null}}$）。

---

## 二、 静态子类型偏序判定（$T_1 <: T_2$）

在值流转与契约消费位置（包括 `use: workflow:<pkg_path>` 传参与返回值绑定、`builtin:*` 算子入参/出参校验及 `for_each` 一阶集合提升目标绑定），编译器采用**单向静态子类型偏序关系（$T_{\text{actual}} <: T_{\text{expected}}$）**。由于 Axcutor 栈帧与容器算子均遵循纯函数式写时复制（Copy-on-Write）不可变语义，不存在原地可变别名引用，`Array`、`OrderedMap` 与 `TypedDict` 均支持安全的结构化协变（Structural Covariance）。

设 $T_1, T_2$ 为满足 [`01-typespec-well-formedness.md`](./01-typespec-well-formedness.md) 的两个良构 `TypeSpec`。定义 $T_1$ 是 $T_2$ 的子类型（记作 $T_1 <: T_2$），当且仅当递归满足以下五条公理：

### 1. 公理 1：可空性单调提升（Nullability Monotonicity）
$$\text{Nullable}(T_1) \implies \text{Nullable}(T_2)$$

* **推论 1.1（非空类型向可空类型单调提升，Corollary: Non-Null to Nullable Promotion）**：对任意良构 `TypeSpec` $T$，由 [`01-typespec-well-formedness.md`](./01-typespec-well-formedness.md) 中 $\text{MakeNullable}(T)$ 的定义知 $\text{BaseType}(\text{MakeNullable}(T)) = \text{BaseType}(T)$ 且 $\text{Nullable}(\text{MakeNullable}(T)) = \texttt{true}$，结合结构自反性可得：
  $$T <: \text{MakeNullable}(T)$$
  因此，非空类型（$\text{Nullable}(T_1) = \texttt{false}$）既可作为同结构非空类型（$\text{Nullable}(T_2) = \texttt{false}$）的子类型，也可单调提升为显式可空类型（$\text{Nullable}(T_2) = \texttt{true}$）的子类型。
* **推论 1.2（可空类型逆向封闭性，Corollary: Prohibition of Implicit Null-Stripping）**：若 $\text{Nullable}(T_1) = \texttt{true}$ 且 $\text{Nullable}(T_2) = \texttt{false}$，则由公理 1 的逆否命题直接导出 $T_1 \not<: T_2$。显式可空类型**只能**作为显式可空类型的子类型，**严禁**直接赋值给非空目标类型（必须先经 `builtin:unwrap` 算子显式拆包）。

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
当 $T_1, T_2$ 均为 `OrderedMap`（$\text{BaseType}(T_1) = \text{BaseType}(T_2) = \texttt{object}$ 且含 `additionalProperties`）时，在满足公理 1 的基础上，值类型递归满足协变：
$$T_1.\text{additionalProperties} <: T_2.\text{additionalProperties}$$

### 5. 公理 5：闭合结构体属性协变与必填性强化（`TypedDict` Covariance）
当 $T_1, T_2$ 均为 `TypedDict`（$\text{BaseType}(T_1) = \text{BaseType}(T_2) = \texttt{object}$ 且含 `properties`）时，在满足公理 1 的基础上，须同时满足以下两条形式化条件：
1. **属性域受限与必填集逆变包含**：
   $$\text{set}(T_2.\text{required}) \subseteq \text{set}(T_1.\text{required}) \;\land\; \text{dom}(T_1.\text{properties}) \subseteq \text{dom}(T_2.\text{properties})$$
2. **共有属性类型递归协变**：
   * 对每个属性 $f \in \text{dom}(T_1.\text{properties})$，须满足：
     $$T_1.\text{properties}[f] <: T_2.\text{properties}[f]$$

* **公理说明 5.1（闭合结构体语义与宽度子类型限制，Remark: Closed-Record Semantics）**：由 [`01-typespec-well-formedness.md`](./01-typespec-well-formedness.md) 形态 A 的闭合结构体公理，`TypedDict` 在运行期校验时隐式视为 `additionalProperties: false`，禁止携带未在目标 Schema `properties` 中声明的额外字段。因此，Axcutor **不支持**丢弃多余字段的传统宽度子类型（Width Subtyping），而是要求 $\text{dom}(T_1.\text{properties}) \subseteq \text{dom}(T_2.\text{properties})$，同时支持**字段深度协变（Depth Subtyping）**与**必填性强化（Requiredness Strengthening）**。
* **推论 5.2（必填性强化与可选字段裁剪，Corollary: Requiredness Strengthening & Optional Field Omission）**：由条件 1 与 [`01-typespec-well-formedness.md`](./01-typespec-well-formedness.md) 的良构性约束 $\text{set}(T_i.\text{required}) \subseteq \text{dom}(T_i.\text{properties})$ 可导出：
  $$\text{set}(T_2.\text{required}) \subseteq \text{set}(T_1.\text{required}) \subseteq \text{dom}(T_1.\text{properties}) \subseteq \text{dom}(T_2.\text{properties})$$
  这意味着父类型 $T_2$ 要求必填的字段在子类型 $T_1$ 中也必须声明且必填；同时，子类型 $T_1$ 允许将 $T_2$ 中的可选字段（$f \in \text{dom}(T_2.\text{properties}) \setminus \text{set}(T_2.\text{required})$）强化为必填字段（加入 $\text{set}(T_1.\text{required})$），或从 $\text{dom}(T_1.\text{properties})$ 中完全省略该可选字段。
* **推论 5.3（键可选性与值可空性正交，Corollary: Orthogonality of Key Optionality and Value Nullability）**：若可选属性 $f \notin \text{set}(T_2.\text{required})$ 在父类型中声明为非空类型（$\text{Nullable}(T_2.\text{properties}[f]) = \texttt{false}$），其在 JSON Schema 契约中表示“键 $f$ 要么在对象中缺省，要么存在且值非 `null`”。因此，若子类型 $T_1$ 保留了属性 $f \in \text{dom}(T_1.\text{properties})$，由条件 2 与公理 1 可知必须满足 $\text{Nullable}(T_1.\text{properties}[f]) = \texttt{false}$，禁止将可空属性类型（$\text{Nullable}(T_1.\text{properties}[f]) = \texttt{true}$）赋给非空的可选目标属性。

> **反对称性定理（Antisymmetry）与实例保持性定理（Value Preservation）**：
> * **反对称性**：$T_1 \equiv T_2 \iff (T_1 <: T_2 \;\land\; T_2 <: T_1)$，即 $(\text{TypeSpec}, <:)$ 在结构化等价商集上构成偏序集（Poset）。
> * **实例保持性**：$\forall v, \; (v \models T_{\text{actual}} \;\land\; T_{\text{actual}} <: T_{\text{expected}}) \implies v \models T_{\text{expected}}$。任何符合子类型 $T_{\text{actual}}$ 的运行期 JSON 实例，无需任何运行期格式转换，天然符合父类型 $T_{\text{expected}}$ 的 JSON Schema 契约。

---

## 三、 字面量 Schema 实例兼容性校验（$L \models T$）

当在 `graph.variables` 初值声明或 `use.with.input` 常量传参中向良构目标类型 $T$ 提供字面量 $L$ 时，基于 [`01-typespec-well-formedness.md`](./01-typespec-well-formedness.md) 定义的 $\text{BaseType}(T)$ 与 $\text{Nullable}(T)$ 执行实例兼容性判定 $L \models T$：

1. **空值 `null` 规则**：
   * 当 $L = \texttt{null}$ 时：$L \models T \iff \text{Nullable}(T) = \texttt{true}$。非空类型（$\text{Nullable}(T) = \texttt{false}$）禁止接受 `null` 字面量。
2. **非空字面量规则（当 $L \neq \texttt{null}$ 时）**：
   * **标量匹配**：
     * $\text{BaseType}(T) = \texttt{string} \implies L$ 必须为字符串；
     * $\text{BaseType}(T) = \texttt{integer} \implies L$ 必须为词法整数字面量（带小数点或指数的浮点数字面量如 `1.0` 不满足 `integer`，布尔值 `true` / `false` 亦不视为整数）；
     * $\text{BaseType}(T) = \texttt{number} \implies L$ 允许为**整数字面量**（如 `0`）或**有限浮点数字面量**（如 `0.0`，布尔值不视为数值）；
     * $\text{BaseType}(T) = \texttt{boolean} \implies L$ 必须为布尔值 `true` 或 `false`；
     * 若 $T$ 声明了 `enum`，则还必须满足 $L \in T.\text{enum}$。
   * **列表字面量匹配（$\text{BaseType}(T) = \texttt{array}$）**：
     * $L$ 必须为 JSON 数组，且对每个元素 $e \in L$，递归满足 $e \models T.\text{items}$（空数组字面量 `[]` 对任意良构 `array` 类型均满足兼容性）。
   * **有序映射字面量匹配（$T$ 为 `OrderedMap`）**：
     * $L$ 必须为 JSON 对象，且对每个值 $v \in \text{values}(L)$，递归满足 $v \models T.\text{additionalProperties}$（空对象字面量 `{}` 对任意良构 `OrderedMap` 均满足兼容性）。
   * **结构体字面量匹配（$T$ 为 `TypedDict`）**：
     * $L$ 必须为 JSON 对象，$\text{set}(T.\text{required}) \subseteq \text{dom}(L) \subseteq \text{dom}(T.\text{properties})$，且对每个键 $k \in \text{dom}(L)$，递归满足 $L[k] \models T.\text{properties}[k]$。
