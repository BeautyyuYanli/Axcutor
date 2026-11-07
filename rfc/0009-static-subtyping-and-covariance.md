# RFC-0009: 静态子类型赋值兼容性与不可变容器协变规则（Subtyping & Covariance）

* **状态**：Proposed
* **所属分类**：类型系统完备性与主流语言能力对齐（Type System Soundness & Expressiveness）
* **核心解决的问题**：
  1. 在保留“同帧同名同类型不变式（$\Sigma_G(v) \equiv T$）”的前提下，引入形式化**静态子类型偏序关系（$T_{\text{sub}} <: T_{\text{sup}}$）**，解决现行规范在所有赋值与传参边界强制要求双向等价（$T_1 \equiv T_2$）导致的表达力僵化问题；
  2. 支持**非空向可空提升**（$T <: \text{MakeNullable}(T)$）、**数值拓宽**（$\texttt{integer} <: \texttt{number}$）与**枚举子集拓宽**（子枚举 $<:$ 父枚举 $<:$ 无 `enum` 基础标量）；
  3. 基于 Axcutor 纯函数式写时复制（Copy-on-Write）不可变内存模型，形式化确立 `Array<T>`、`OrderedMap<string, V>` 与 `TypedDict` 的**安全结构化协变（Structural Covariance）**。
* **关联规范模块（`manual/spec/`）**：
  * [`index.md`](../manual/spec/index.md)
  * [`02-type-system-and-symbols/02-type-equivalence-and-compatibility.md`](../manual/spec/02-type-system-and-symbols/02-type-equivalence-and-compatibility.md)
  * [`02-type-system-and-symbols/03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md)
  * [`02-type-system-and-symbols/05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md)
* **关联文档模块（`manual/docs/`）**：
  * [`guide/01-foundations/01-basics.md`](../manual/docs/guide/01-foundations/01-basics.md)
  * [`guide/01-foundations/02-use-subworkflows.md`](../manual/docs/guide/01-foundations/02-use-subworkflows.md)
  * [`reference/03-typespec-cheatsheet.md`](../manual/docs/reference/03-typespec-cheatsheet.md)

---

## 一、 问题背景与“全等价类型检查”引发的工程痛点

在现行规范 [`02-type-equivalence-and-compatibility.md`](../manual/spec/02-type-system-and-symbols/02-type-equivalence-and-compatibility.md) 中，编译期只定义了两种类型关系：
1. 两个 `TypeSpec` 之间的**双向结构化等价（$T_1 \equiv T_2$）**；
2. 常量字面量 $L$ 对目标类型 $T$ 的**实例兼容性（$L \models T$）**。

由于缺乏两个 `TypeSpec` 之间的**单向子类型关系（$T_{\text{actual}} <: T_{\text{expected}}$）**，现行规范在所有跨变量赋值、内建算子入参校验、子工作流调用传参（`use.with.input`）与返回值接收（`use.with.output`）位置，一律要求源类型与目标类型满足严格的双向等价 $T_{\text{actual}} \equiv T_{\text{expected}}$。

这导致在 TypeScript、Rust、Python、Go 等主流编程语言中完全合法且类型安全的四类常见操作，在 Axcutor 中全部被静态检查器拒绝：

---

### 痛点 9.1：非空类型 `T` 无法传入或赋值给可空目标类型 `[T, "null"]`

* **场景**：
  * 子工作流 `workflow:notify_user` 声明了一个可选输入参数 `cc_email: { type: ["string", "null"] }`；
  * 或图内声明了一个初始为 `null` 的可空局部变量 `best_candidate: { type: ["string", "null"], default: null }`，后续在某分支中尝试通过 `builtin:assign` 将一个已确认非空的变量 `verified_user: { type: "string" }` 赋值给 `best_candidate`。
* **现状报错**：
  * 因为 $\text{Nullable}(\texttt{string}) = \texttt{false} \neq \text{Nullable}([\texttt{string}, \texttt{"null"}]) = \texttt{true}$，两者不满足 $T_1 \equiv T_2$，编译期直接报错！
  * 用户居然无法把一个 guaranteed 非空的 `string` 变量放进一个允许为 `string | null` 的变量或参数槽位中。

---

### 痛点 9.2：带 `enum` 约束的精确类型无法传给普通标量或更宽的父枚举

* **场景**：
  * 节点 `classify_ticket` 产出了状态变量 `ticket_status: { type: "string", enum: ["open", "closed"] }`；
  * 下游希望将 `ticket_status` 传给通用的日志记录子工作流（入参类型为无 `enum` 的普通 `{ type: "string" }`），或赋值给全生命周期状态变量 `lifecycle_status: { type: "string", enum: ["draft", "open", "closed", "archived"] }`。
* **现状报错**：
  * 现行规则要求 $\text{set}(T_1.\text{enum}) = \text{set}(T_2.\text{enum})$，导致任何带 `enum` 的变量一旦产出，就成了“类型孤岛”，连最普通的 `type: string` 参数都传不进去。

---

### 痛点 9.3：`integer` 变量与 `number` 目标类型之间的割裂

* **场景**：
  * 在 [`02-type-equivalence-and-compatibility.md`](../manual/spec/02-type-system-and-symbols/02-type-equivalence-and-compatibility.md#L35) 中，字面量兼容性规则 $L \models T$ 明确规定：“$\text{BaseType}(T) = \texttt{number} \implies L$ 允许为**整数字面量**（如 `0`）或**浮点数字面量**（如 `0.0`）”；
  * 但在变量与变量之间，却规定 $\texttt{integer} \not\equiv \texttt{number}$。
* **现状悖论**：
  * 用户可以直接把整数字面量 `10` 传给 `type: number` 的目标参数；但如果先把 `10` 存入 `count: { type: "integer", default: 10 }`，再把 `count` 变量传给同一个 `type: number` 的目标参数，编译期就会报错！
  * 在 JSON 数据模型（RFC 8259）与 JSON Schema 规范中，`integer` 的数学值域是 `number` 值域的严格真子集（$\mathbb{Z} \subset \mathbb{R}$）。

---

### 痛点 9.4：不可变容器（`Array`、`OrderedMap`、`TypedDict`）无法随元素子类型安全协变

* **场景**：
  * 某节点产出了非空枚举列表 `Array<string(enum: ["a", "b"])>`，无法传给期望 `Array<string>` 的子工作流；
  * 某结构体变量 `user` 的字段 `nickname` 为非空 `string`，无法通过 `builtin:assign` 赋值给期望 `nickname` 为可空 `["string", "null"]` 或可选属性（非 `required`）的同构目标结构体变量。
* **背景分析**：
  * 在 Java/TypeScript 等具备**原地可变引用（In-Place Mutation）**的语言中，可变数组协变会导致著名的写入类型污染漏洞（如将 `Dog[]` 当作 `Animal[]` 传入后向其原地 `push(new Cat())`）；
  * 但在 Axcutor 中，根据 [`01-frame-memory-model.md`](../manual/spec/04-runtime-execution-model/01-frame-memory-model.md) 与 [`03-guardrails-and-step-accounting.md`](../manual/spec/04-runtime-execution-model/03-guardrails-and-step-accounting.md#L44-L46)，**所有栈帧快照与容器算子均采用纯函数式写时复制（Copy-on-Write Immutability）语义，不存在任何原地可变别名引用**。在纯不可变值语义下，列表、有序映射与结构体的结构化协变（Covariance）在数学上是 **100% 类型安全（Sound）** 的。

---

## 二、 核心设计决策

### 1. 严格区分“符号表声明不变式（$\equiv$）”与“单向流转赋值兼容性（$<:$）”

为确保结合 [`RFC-0007`](./0007-explicit-symbol-declarations-and-type-checking-simplification.md) 的单遍显式符号表构建具有唯一确定性，规范严格区分两类类型关系的使用边界：

1. **符号表同帧同名类型声明不变式——保持双向等价（$T_1 \equiv T_2$）**：
   * 在阶段 1 构建图级符号表 $\Sigma_G$ 时，若同一个顶层变量名 $v$ 在多处显式声明了 `TypeSpec`（如同时出现在 `root.input` 与 `ActionNode.output`，或同时出现在两个不同 `ActionNode` 的显式 `output` 声明中），两处声明的 `TypeSpec` 必须满足**双向静态等价 $T_1 \equiv T_2$**。
   * 这保证了每个变量 $v \in \text{dom}(\Sigma_G)$ 在所属栈帧中拥有**唯一、精确、与节点遍历顺序无关的权威静态类型 $\Sigma_G(v)$**。
2. **值流转与契约消费位置——采用单向子类型兼容（$T_{\text{actual}} <: T_{\text{expected}}$）**：
   * 在阶段 2 校验数据从源表达式/源变量流向目标参数或已声明目标变量时，统一采用单向子类型关系 $T_{\text{actual}} <: T_{\text{expected}}$。
   * **实例保持性定理（Value Preservation Theorem）**：
     $$\forall v, \quad (v \models T_{\text{actual}} \;\land\; T_{\text{actual}} <: T_{\text{expected}}) \implies v \models T_{\text{expected}}$$
     即任何符合子类型 $T_{\text{actual}}$ 的运行期 JSON 实例，无需任何运行期格式转换，天然 100% 符合父类型 $T_{\text{expected}}$ 的 JSON Schema 契约。

---

### 2. 静态子类型偏序关系（$T_1 <: T_2$）的形式化公理

设 $T_1, T_2$ 为两个良构的 `TypeSpec`。定义 $T_1$ 是 $T_2$ 的子类型（记作 $T_1 <: T_2$），当且仅当递归满足以下条件：

#### 公理 1：可空性单调提升（Nullability Monotonicity）
$$\text{Nullable}(T_1) \implies \text{Nullable}(T_2)$$
* 即：
  * 非空类型（$\text{Nullable}(T_1) = \texttt{false}$）可以作为非空类型（$\text{Nullable}(T_2) = \texttt{false}$）或可空类型（$\text{Nullable}(T_2) = \texttt{true}$）的子类型；
  * 可空类型（$\text{Nullable}(T_1) = \texttt{true}$）**只能**作为可空类型（$\text{Nullable}(T_2) = \texttt{true}$）的子类型，**严禁**赋值给非空目标类型（必须先经 `builtin:unwrap` 拆包）。

#### 公理 2：标量基础类型与枚举子集拓宽（Scalar & Enum Widening）
当 $T_1, T_2$ 为标量类型时，在满足公理 1 的基础上：
1. **基础类型兼容**：
   * 要么 $\text{BaseType}(T_1) = \text{BaseType}(T_2) \in \{\texttt{string}, \texttt{integer}, \texttt{number}, \texttt{boolean}\}$；
   * 要么 $\text{BaseType}(T_1) = \texttt{integer} \;\land\; \text{BaseType}(T_2) = \texttt{number}$（整数向浮点数/通用数值拓宽）。
2. **枚举约束拓宽**：
   * 若 $T_2$ 未声明 `enum`：无论 $T_1$ 是否声明 `enum`，均满足兼容；
   * 若 $T_2$ 声明了 `enum`：则 $T_1$ **也必须声明 `enum`**，且满足枚举值集合包含关系：
     $$\text{set}(T_1.\text{enum}) \subseteq \text{set}(T_2.\text{enum})$$

#### 公理 3：强类型列表协变（`Array` Covariance）
当 $\text{BaseType}(T_1) = \text{BaseType}(T_2) = \texttt{array}$ 时，在满足公理 1 的基础上，元素类型递归满足协变：
$$T_1.\text{items} <: T_2.\text{items}$$

#### 公理 4：强类型有序映射协变（`OrderedMap` Covariance）
当 $T_1, T_2$ 均为 `OrderedMap`（`type: object` + `additionalProperties`）时，在满足公理 1 的基础上，值类型递归满足协变：
$$T_1.\text{additionalProperties} <: T_2.\text{additionalProperties}$$

#### 公理 5：闭合结构体属性协变与必填性强化（`TypedDict` Covariance）
当 $T_1, T_2$ 均为 `TypedDict`（`type: object` + `properties`）时，在满足公理 1 的基础上：
* *注：由于 Axcutor 的 `TypedDict` 在运行时视为 `additionalProperties: false`（禁止携带未在目标 Schema `properties` 中声明的多余字段，详见 [`01-typespec-well-formedness.md`](../manual/spec/02-type-system-and-symbols/01-typespec-well-formedness.md#L48)），因此结构体不支持丢弃额外字段的宽度子类型（Width Subtyping），但支持**字段类型深度协变（Depth Subtyping）**与**必填性强化（Required Strengthening）***：
1. **属性域在目标声明范围内且覆盖目标全部必填项**：
   $$\text{set}(T_2.\text{required}) \subseteq \text{set}(T_1.\text{required}) \;\land\; \text{dom}(T_1.\text{properties}) \subseteq \text{dom}(T_2.\text{properties})$$
   *(即：$T_2$ 要求必填的字段在 $T_1$ 中也必须必填；$T_1$ 允许把 $T_2$ 中的可选字段强化为必填字段，或完全省略 $T_2$ 中的非必填字段。)*
2. **共有属性类型递归协变**：
   * 对每个属性 $f \in \text{dom}(T_1.\text{properties})$：
     * 若 $f \in \text{set}(T_2.\text{required})$：须满足 $T_1.\text{properties}[f] <: T_2.\text{properties}[f]$；
     * 若 $f \notin \text{set}(T_2.\text{required})$（目标属性在 $T_2$ 中为可选属性）：须满足 $T_1.\text{properties}[f] <: T_2.\text{properties}[f]$（注意：若 $T_2.\text{properties}[f]$ 本身声明为非空类型但未列入 $T_2.\text{required}$，在 JSON Schema 中意味着“键要么省略、要么存在且非 `null`”，因此 $T_1.\text{properties}[f]$ 不可将 `null` 赋给非空的 $T_2.\text{properties}[f]$）。

> **反对称性推论（Antisymmetry）**：
> 由上述公理直接可得：$T_1 \equiv T_2 \iff (T_1 <: T_2 \;\land\; T_2 <: T_1)$。因此 $(TypeSpec, <:)$ 构成一个严格的偏序集（Poset）。

---

### 3. 各语法与算子检查位置的适用规则汇总表

| 检查场景 | 现行规则 | 修订后规则 | 说明 |
| :--- | :--- | :--- | :--- |
| **同帧多处显式声明同一变量 $v$** | $T_1 \equiv T_2$ | **$T_1 \equiv T_2$（不变）** | 确保符号表 $\Sigma_G(v)$ 唯一且与声明收集顺序无关 |
| **`use: workflow:<path>` 的 `with.input.<p>`** | $T_{\text{arg}} \equiv \text{target.input}[p]$ | **$T_{\text{arg}} <: \text{target.input}[p]$** | 允许向子工作流传入更精确的子类型（如非空传给可空、子枚举传给 `string`） |
| **`use: workflow:<path>` 的 `with.output.<f>: v`** | $\text{target.output}[f] \equiv \Sigma_G(v)$ | **$\text{target.output}[f] <: \Sigma_G(v)$** | 允许父图用更宽泛的变量类型接收子工作流的精确输出 |
| **`builtin:assign`（`source` $\to$ `value`）** | $T_{\text{src}} \equiv \Sigma_G(v_{\text{out}})$ | **$T_{\text{src}} <: \Sigma_G(v_{\text{out}})$** | 允许将非空变量、`integer` 或子枚举赋值给可空变量、`number` 或父枚举变量 |
| **`builtin:unwrap`（`source`, `default` $\to$ `value`）** | $\text{StripNull}(T_{\text{src}}) \equiv \Sigma_G(v_{\text{out}})$<br>$T_{\text{def}} \equiv \Sigma_G(v_{\text{out}})$ | **$\text{StripNull}(T_{\text{src}}) <: \Sigma_G(v_{\text{out}})$**<br>**$T_{\text{def}} <: \Sigma_G(v_{\text{out}})$** | 拆包后的非空值与默认值均可子类型兼容写入 $v_{\text{out}}$（仍要求 $\text{Nullable}(\Sigma_G(v_{\text{out}})) = \texttt{false}$） |
| **`builtin:array_shift`（`source` $\to$ `item`, `rest`）** | $T_{\text{elem}} \equiv \Sigma_G(v_{\text{item}})$<br>$T_{\text{src}} \equiv \Sigma_G(v_{\text{rest}})$ | **$T_{\text{elem}} <: \Sigma_G(v_{\text{item}})$**<br>**$T_{\text{src}} <: \Sigma_G(v_{\text{rest}})$** | 弹出元素与剩余切片支持子类型赋入目标变量 |
| **`builtin:array_append`（`source`, `item` $\to$ `result`）** | $T_{\text{src}} \equiv \Sigma_G(v_{\text{res}})$<br>$T_{\text{item}} \equiv T_{\text{elem}}$ | **$T_{\text{src}} <: \Sigma_G(v_{\text{res}})$**<br>**$T_{\text{item}} <: \Sigma_G(v_{\text{res}}).\text{items}$** | 允许向 `Array<number>` 追加 `integer`，或向 `Array<[string, "null"]>` 追加非空 `string` |
| **`builtin:map_set`（`source`, `key`, `value` $\to$ `result`）** | $T_{\text{src}} \equiv \Sigma_G(v_{\text{res}})$<br>$T_{\text{val}} \equiv T_V$ | **$T_{\text{src}} <: \Sigma_G(v_{\text{res}})$**<br>**$T_{\text{val}} <: \Sigma_G(v_{\text{res}}).\text{additionalProperties}$** | 允许向映射写入子类型值 |
| **`builtin:map_get`（`source`, `default` $\to$ `value`）** | $T_V \equiv \Sigma_G(v_{\text{out}})$<br>$T_{\text{def}} \equiv T_V$ | **$T_V <: \Sigma_G(v_{\text{out}})$**<br>**$T_{\text{def}} <: \Sigma_G(v_{\text{out}})$** | 查出的值与 `default` 均可子类型兼容写入 $v_{\text{out}}$ |
| **`builtin:map_delete`（`source` $\to$ `result`）** | $T_{\text{src}} \equiv \Sigma_G(v_{\text{res}})$ | **$T_{\text{src}} <: \Sigma_G(v_{\text{res}})$** | 删除键后的映射支持子类型赋入目标变量 |
| **`builtin:object_merge`（`base`, `f_i` $\to$ `result`）** | $T_{\text{base}} \equiv \Sigma_G(v_{\text{res}})$<br>$T_i \equiv T_{\text{prop}, i}$ | **$T_i <: \Sigma_G(v_{\text{res}}).\text{properties}[f_i]$** | 各覆盖/构造字段支持直接传入子类型（天然涵盖非空字段传入可空属性） |
| **`for_each.filter` 与 `for_each.collect`** | $T_{\text{derived}} \equiv \Sigma_G(v_{\text{target}})$ | **$T_{\text{derived}} <: \Sigma_G(v_{\text{target}})$** | 收集或过滤产出的集合类型可子类型兼容写入外层目标集合变量 |

---

## 三、 `manual/spec/` 形式化规范逐文件修改方案

### 1. [`02-type-system-and-symbols/02-type-equivalence-and-compatibility.md`](../manual/spec/02-type-system-and-symbols/02-type-equivalence-and-compatibility.md)

* 将文件标题由 `# 02. 静态类型等价判定与字面量实例兼容性算法` 扩展为：
  `# 02. 静态类型等价、子类型偏序与字面量实例兼容性算法`
* 在第一节“静态类型结构化等价判定（$T_1 \equiv T_2$）”之后，新增**“第二节：静态子类型偏序判定（$T_1 <: T_2$）”**，完整写入本文第二.2 节的五条递归公理（可空性单调提升、标量与枚举拓宽、列表协变、有序映射协变、`TypedDict` 属性协变与必填性强化）及反对称性定理。
* 原第二节顺延为“第三节：字面量 Schema 实例兼容性校验（$L \models T$）”。

---

### 2. [`02-type-system-and-symbols/03-two-phase-symbol-resolution.md`](../manual/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution.md)

* 在阶段 1（构建符号表 $\Sigma_G$）中，保持同帧同名声明的双向静态等价校验 $\Sigma_G(v) \equiv T$；
* 在阶段 2（节点级静态类型检查）中：
  * **`CallNode`（`use: workflow:<path>`）**：
    * 入参引用 $E$ 校验更新为：$\text{TypeOf}(\Sigma, E) <: \text{target.input}[p]$；
    * 出参绑定 `f: local_var` 校验更新为：$\text{target.output}[f] <: \Sigma_G(\text{local\_var})$；
  * **挂载 `for_each` 的节点 $M$**：
    * `filter` 目标变量 $v_{\text{flt}}$ 校验更新为：$T_{\text{src}} <: \Sigma_G(v_{\text{flt}})$；
    * `collect` 目标变量 $v_{\text{col}}$ 校验更新为：提升后的集合类型 $T_{\text{collected}} <: \Sigma_G(v_{\text{col}})$。

---

### 3. [`02-type-system-and-symbols/05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md)

按本文第二.3 节汇总表，更新各算子的类型校验公式：

1. **`builtin:unwrap`**：
   * 设 $T = \text{StripNull}(T_{\text{src}})$，目标变量 $v_{\text{out}}$ 必须满足 $\text{Nullable}(\Sigma(v_{\text{out}})) = \texttt{false}$ 且 $T <: \Sigma(v_{\text{out}})$；
   * 若声明了 `default`：当为引用 $E_{\text{def}}$ 时须满足 $\text{TypeOf}(\Sigma, E_{\text{def}}) <: \Sigma(v_{\text{out}})$，当为字面量 $L_{\text{def}}$ 时须满足 $L_{\text{def}} \models \Sigma(v_{\text{out}})$。
2. **`builtin:assign`**：
   * 当 `source` 为引用 $E$ 时，须满足 $\text{TypeOf}(\Sigma, E) <: \Sigma(v_{\text{out}})$；当为字面量 $L$ 时，须满足 $L \models \Sigma(v_{\text{out}})$。
3. **`builtin:array_shift`**：
   * 设 $T_{\text{src}} = \{ \texttt{type}: \texttt{array}, \texttt{items}: T \}$，须满足 $T <: \Sigma(v_{\text{item}})$ 且 $T_{\text{src}} <: \Sigma(v_{\text{rest}})$。
4. **`builtin:array_append`**：
   * 设目标类型 $T_{\text{res}} = \Sigma(v_{\text{res}}) = \{ \texttt{type}: \texttt{array}, \texttt{items}: T_{\text{target}} \}$（$\text{Nullable}(T_{\text{res}}) = \texttt{false}$），须满足 $T_{\text{src}} <: T_{\text{res}}$，且 `item` 满足 $\text{TypeOf}(\Sigma, \text{item}) <: T_{\text{target}}$（或字面量 $L \models T_{\text{target}}$）。
5. **`builtin:map_set`**：
   * 设目标类型 $T_{\text{res}} = \Sigma(v_{\text{res}}) = \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: T_{V,\text{target}} \}$（$\text{Nullable}(T_{\text{res}}) = \texttt{false}$），须满足 $T_{\text{src}} <: T_{\text{res}}$，`key`满足 $\text{TypeOf}(\Sigma, \text{key}) <: \{ \texttt{type}: \texttt{string} \}$，且 `value` 满足 $\text{TypeOf}(\Sigma, \text{value}) <: T_{V,\text{target}}$（或字面量 $L \models T_{V,\text{target}}$）。
6. **`builtin:map_get`**：
   * 设 $T_{\text{src}} = \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: T_V \}$，须满足 $T_V <: \Sigma(v_{\text{out}})$，`key` 满足 $\text{TypeOf}(\Sigma, \text{key}) <: \{ \texttt{type}: \texttt{string} \}$；若声明了 `default`，须满足 $\text{TypeOf}(\Sigma, E_{\text{def}}) <: \Sigma(v_{\text{out}})$（或字面量 $L_{\text{def}} \models \Sigma(v_{\text{out}})$）。
7. **`builtin:map_delete`**：
   * 须满足 $T_{\text{src}} <: \Sigma(v_{\text{res}})$ 且 `key` 满足 $\text{TypeOf}(\Sigma, \text{key}) <: \{ \texttt{type}: \texttt{string} \}$。
8. **`builtin:object_merge`**：
   * 设目标类型 $T_{\text{target}} = \Sigma(v_{\text{res}})$（非空 `TypedDict`）：
   * 对每个覆盖或构造字段 $f_i \in \text{dom}(T_{\text{target}}.\text{properties})$（记 $T_{\text{prop}, i} = T_{\text{target}}.\text{properties}[f_i]$）：若绑定引用 $E_i$，统一校验 $\text{TypeOf}(\Sigma, E_i) <: T_{\text{prop}, i}$（由于公理 1 已涵盖非空向可空提升，无需再写额外的 `MakeNullable` 特判）；若绑定字面量 $L_i$，校验 $L_i \models T_{\text{prop}, i}$；
   * 若声明了 `base`（记 $T_{\text{base}} = \text{TypeOf}(\Sigma, \text{with.input.base})$），校验 $\text{dom}(T_{\text{base}}.\text{properties}) \subseteq \text{dom}(T_{\text{target}}.\text{properties})$、$\text{set}(T_{\text{target}}.\text{required}) \subseteq \text{set}(T_{\text{base}}.\text{required}) \cup \{f_1, \dots, f_m\}$，且对未覆盖的保留字段 $f \in \text{dom}(T_{\text{base}}.\text{properties}) \setminus \{f_1, \dots, f_m\}$ 满足 $T_{\text{base}}.\text{properties}[f] <: T_{\text{target}}.\text{properties}[f]$。
