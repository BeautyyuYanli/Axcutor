# 02. 静态类型等价判定与字面量实例兼容性算法

本规范定义编译期判定两个 `TypeSpec` 静态等价（$T_1 \equiv T_2$）的形式化算法，以及判定常量字面量 $L$ 兼容于目标类型 $T$（$L \models T$）的实例校验规则。

---

## 一、 静态类型结构化等价判定（$T_1 \equiv T_2$）

两个良构的 `TypeSpec` $T_1$ 与 $T_2$ 静态等价（记作 $T_1 \equiv T_2$），当且仅当递归满足以下全部条件：

1. **基础类型与可空性一致**：
   * $\text{BaseType}(T_1) = \text{BaseType}(T_2)$（注意：在变量与变量之间，$\texttt{integer} \not\equiv \texttt{number}$，不存在隐式子类型转换）；
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

## 二、 字面量 Schema 实例兼容性校验（$L \models T$）

当在 `graph.variables` 初值声明或 `use.with.input` 常量传参中向目标类型 $T$ 提供字面量 $L$ 时，执行实例兼容性判定 $L \models T$：

1. **空值 `null` 规则**：
   * 当 $L = \texttt{null}$ 时：$L \models T \iff \text{Nullable}(T) = \texttt{true}$。非空类型严禁接受 `null` 字面量。
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
