# 01. `TypeSpec` 良构性公理（Well-Formedness Rules）

Axcutor 的类型系统采用标准 JSON Schema 的**闭合强类型子集（`TypeSpec`）**。静态检查器在编译期首先对所有出现的 `TypeSpec` 执行良构性校验（Well-Formedness Validation），不满足以下公理的 Schema 在编译期被拒绝。

---

## 一、 基础类型域与可空性修饰公理

设基础非空类型集合为：
$$\mathcal{B} = \{\texttt{"string"}, \texttt{"integer"}, \texttt{"number"}, \texttt{"boolean"}, \texttt{"array"}, \texttt{"object"}\}$$

1. **禁止 `any`、裸 `"null"` 与多类型联合**：
   * 每个 `TypeSpec` 必须包含 `type` 字段。
   * `type` 的值只能是以下两种形式之一：
     * **非空类型**：$b \in \mathcal{B}$；
     * **显式可空类型（`Optional<T>`）**：长度为 2 的数组 $[b, \texttt{"null"}]$ 或 $[\texttt{"null"}, b]$，其中 $b \in \mathcal{B}$。
   * 禁止省略 `type`、禁止 `type: "null"` 单独出现、禁止两个不同基础类型联合（如 `["string", "integer"]`）。

---

## 二、 四类构造器的递归闭合性公理

根据基础类型 $b \in \mathcal{B}$，`TypeSpec` 必须满足对应的构造器约束：

### 1. 基础标量构造器（$b \in \{\texttt{"string"}, \texttt{"integer"}, \texttt{"number"}, \texttt{"boolean"}\}$）
* 允许可选字段 `enum: List<Literal>` 与 `description: String`；禁止出现 `items`、`properties`、`required`、`additionalProperties`。
* 若声明了 `enum`：
  * `enum` 必须为非空且无重复元素的字面量数组；
  * `enum` 中的每一个元素必须属于基础类型 $b$（不允许包含 `null`，可空性统一由 `type: [b, "null"]` 表达）。

### 2. 强类型列表构造器（$b = \texttt{"array"}$）
* **必填字段**：必须声明 `items: TypeSpec`，且 `items` 的值必须是一个递归良构的 `TypeSpec`（禁止 `items` 为数组或布尔值）。
* 禁止出现 `properties`、`required`、`additionalProperties`、`enum`。

### 3. 强类型对象构造器（$b = \texttt{"object"}$）—— `TypedDict` 与 `OrderedMap` 互斥约束

凡基础类型为 `object` 的 `TypeSpec`，**必须且只能在以下两种形态中二选一**（禁止同时包含 `properties` 与 `additionalProperties`，也禁止两者同时省略）：

#### 形态 A：强类型结构体（`TypedDict`：`object` + `properties`）
* `properties: Map<Identifier, TypeSpec>` 必须为**非空映射**，每个键必须为合法 `Identifier`，每个值必须为递归良构的 `TypeSpec`。
* `required: List<Identifier>`（可选）：
  * 若省略 `required`，编译器将其规范化为 `required = dom(properties)`（默认全部字段必填）；
  * 若显式声明 `required`，其元素必须无重复且满足 $\text{set}(\text{required}) \subseteq \text{dom}(\text{properties})$。
* 运行时校验时视为 `additionalProperties: false`（闭合结构体，不允许未声明的额外字段）。

#### 形态 B：强类型有序映射（`OrderedMap`：`object` + `additionalProperties`）
* `additionalProperties: TypeSpec` 必须是一个递归良构的 `TypeSpec`（禁止将 `additionalProperties` 设为布尔值 `true` 或 `false`）。
* 禁止出现 `properties` 或 `required`。
