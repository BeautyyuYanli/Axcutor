# RFC-0010: 标量比较、逻辑、算术与数组随机访问内建算子扩展（Scalar & Array Builtins）

* **状态**：Implemented
* **所属分类**：对标主流编程语言的原语与内建算子扩展（Language Expressiveness & Builtin Extensions）
* **核心解决的问题**：
  1. 解决纯图编排中缺乏**标量比较（`==`, `!=`, `<`, `<=`, `>`, `>=`）**、**布尔逻辑组合（`and`, `or`, `not`）**、**基础算术运算（`add`, `sub`, `mul`, `div`, `mod`）**与**集合/字符串长度测量（`length`）**导致的表达力断层，使计数循环（`while (i < n)`）、阈值路由（`score >= 0.85`）、状态累加（`total + price`）无需调用外部 `run` 执行器即可在图内零开销确定性完成；
  2. 补齐强类型列表（`Array<T>`）的随机按索引读取（`builtin:array_get`）与列表拼接（`builtin:array_concat`）能力，消除 `Array` 容器只能从头部破坏性弹出（`array_shift`）或单元素尾部追加（`array_append`）的限制。
* **关联规范模块（`manual/spec/`）**：
  * [`index.md`](../manual/spec/index.md)
  * [`02-type-system-and-symbols/05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md)
  * [`03-static-flow-analysis/03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)
  * [`04-runtime-execution-model/03-guardrails-and-step-accounting.md`](../manual/spec/04-runtime-execution-model/03-guardrails-and-step-accounting.md)
* **关联文档模块（`manual/docs/`）**：
  * [`guide/02-control-flow/01-branches.md`](../manual/docs/guide/02-control-flow/01-branches.md)
  * [`guide/02-control-flow/02-cycles-and-back-edges.md`](../manual/docs/guide/02-control-flow/02-cycles-and-back-edges.md)
  * [`reference/builtins/index.md`](../manual/docs/reference/builtins/index.md)

---

## 一、 问题背景与主流编程语言能力缺口分析

现行规范在 [`05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md) 中定义了 9 个确定性内建算子（`unwrap`、`switch`、`assign`、`array_shift`、`array_append`、`map_set`、`map_get`、`map_delete`、`object_merge`）。

然而，对标主流编程语言的基础运算与标准容器操作，现行内建算子集在以下两个核心维度存在明显断层：

---

### 痛点 10.1：缺乏标量比较、布尔逻辑、算术与长度算子，纯控制流与计数循环被迫逃逸至 `run`

1. **无法表达数值阈值判断与相等性分支**：
   * `builtin:switch` 仅支持 `boolean` 与带静态 `enum` 的 `string`。
   * 面对最基础的业务条件——如 `confidence_score >= 0.85`、`attempt_count < max_retries`、`doc.author_id == current_user_id`、`items_len == 0`——用户无法在图中直接路由，被迫专门编写一个 `run` 节点去计算一个布尔值。
2. **无法在图内执行计数器递增或数值累加**：
   * 在基于 CFG 回边的循环（或移除 `for_each.state` 后的串行折叠循环）中，维护迭代步数 `i = i + 1`、累加总分 `total_score = total_score + item.score` 或计算剩余配额 `remaining = budget - cost` 是最基本的操作。
   * 缺乏算术算子导致哪怕只是给整数加 `1`，也必须调用一次外部 `run` 执行器。
3. **无法组合布尔条件或对布尔值取反**：
   * 当存在多个布尔标志（如 `is_authenticated` 与 `has_quota`）时，缺乏 `and` / `or` / `not` 逻辑算子，只能通过多层串行 `builtin:switch` 节点展开组合。
4. **无法非破坏性地获取 `Array`、`OrderedMap` 或 `string` 的长度**：
   * 目前若想判断列表是否为空或获取列表元素个数，只能调用 `builtin:array_shift` 破坏性地弹出首元素，无法在不拆解列表的情况下读取 `length`。

---

### 痛点 10.2：`Array<T>` 缺乏按索引随机读取（`array_get`）与列表拼接（`array_concat`）

1. **`Array<T>` 无法按索引读取元素**：
   * `OrderedMap<string, V>` 已经拥有非破坏性按键读取算子 `builtin:map_get`（支持 `.some` / `.none` 分支与可选 `default`）；
   * 但 `Array<T>` 却只有队列头弹出算子 `builtin:array_shift`，无法按整数下标 `index`（如读取首元素 `0`、末尾元素 `-1` 或动态下标 `i`）非破坏性地读取元素。
2. **`Array<T>` 无法合并两个列表**：
   * `builtin:array_append` 仅支持向列表末尾追加单个元素 `T`，当需要将两个同构列表 `Array<T>` 首尾拼接（如合并两路并发查询的结果列表）时，无法在单步内完成。

---

## 二、 核心设计决策：新增 6 个正交确定性内建算子

遵循 Axcutor 既有内建算子的设计哲学（**纯函数式不可变值语义、不抛出运行时异常、静态类型检查与必达定值分析完全自洽**，并遵循 [`RFC-0007`](./0007-explicit-symbol-declarations-and-type-checking-simplification.md) 的显式符号表原则与 [`RFC-0009`](./0009-static-subtyping-and-covariance.md) 的子类型规则），新增以下 6 个内建算子：

| 类别 | 算子名称 | 核心职责 | 控制流与出参形态 |
| :--- | :--- | :--- | :--- |
| **标量与谓词** | **`builtin:compare`** | 二元比较（`eq`, `ne`, `lt`, `lte`, `gt`, `gte`） | `with.output` 可选（`result?: boolean`）；当未声明 `with.output` 时直接暴露 `.true` / `.false` 分支，当声明 `result` 时为无分支单出边 |
| **标量与谓词** | **`builtin:logic`** | 布尔逻辑运算（`and`, `or`, `not`） | `with.output` 可选（`result?: boolean`）；支持直接暴露 `.true` / `.false` 分支或输出布尔变量 |
| **标量与谓词** | **`builtin:math`** | 二元数值算术（`add`, `sub`, `mul`, `div`, `mod`） | 针对 `div` / `mod` 除零安全：当未传 `default` 时暴露 `.some` / `.div_by_zero` 分支，其它运算或传入 `default` 时为无分支单出边 |
| **容器与长度** | **`builtin:length`** | 获取非空 `array`、`OrderedMap` 或 `string` 的长度 | 无分支单出边，输出 `result: integer`（$\ge 0$） |
| **列表操作** | **`builtin:array_get`** | 按整数下标（支持负数倒数索引）非破坏性读取列表元素 | 与 `builtin:map_get` 完全同构：未传 `default` 时暴露 `.some` / `.none`（越界时 Kill 输出变量），传 `default` 时为单出边 |
| **列表操作** | **`builtin:array_concat`** | 将两个同构列表首尾拼接为新列表 | 无分支单出边，输出 `result: Array<T>` |

---

### 1. `builtin:compare`（二元比较与条件分支算子）

```yaml
# 形态 A：直接作为条件路由节点（省略 with.output，暴露 .true 与 .false 分支）
check_score:
  use: builtin:compare
  with:
    input:
      op: { value: "gte" }          # "eq" | "ne" | "lt" | "lte" | "gt" | "gte"
      left: review_score
      right: 0.85

# 形态 B：将比较结果存入布尔变量（声明 with.output.result，无分支单出边）
compute_is_passed:
  use: builtin:compare
  with:
    input:
      op: { value: "eq" }
      left: doc.author_id
      right: current_user.id
    output:
      result: is_owner
```

* **入参校验**：
  * `op`（必填）：必须为字符串常量字面量 `{ value: "<op>" }`，取值属于 $\{\texttt{"eq"}, \texttt{"ne"}, \texttt{"lt"}, \texttt{"lte"}, \texttt{"gt"}, \texttt{"gte"}\}$。
  * `left`、`right`（必填）：设其类型为 $T_L, T_R$：
    * **当 `op` $\in \{\texttt{"eq"}, \texttt{"ne"}\}$（相等性比较）**：
      要求 $T_L$ 与 $T_R$ 满足**可比性（Comparable）**——即 $\text{StripNull}(T_L) <: \text{StripNull}(T_R)$ 或 $\text{StripNull}(T_R) <: \text{StripNull}(T_L)$（允许同构标量、列表、结构体或有序映射之间按 JSON 深度值相等比较，且允许可空类型变量与 `{ value: null }` 字面量直接判等）。
    * **当 `op` $\in \{\texttt{"lt"}, \texttt{"lte"}, \texttt{"gt"}, \texttt{"gte"}\}$（序比较）**：
      要求 $\text{Nullable}(T_L) = \texttt{false} \;\land\; \text{Nullable}(T_R) = \texttt{false}$，且满足以下二者之一：
      1. 数值比较：$\text{BaseType}(T_L), \text{BaseType}(T_R) \in \{\texttt{integer}, \texttt{number}\}$；
      2. 字符串字典序比较：$\text{BaseType}(T_L) = \text{BaseType}(T_R) = \texttt{string}$（按 UTF-8 码点字典序比较）。
* **出参与控制分支**：
  * **未声明 `with.output` 时**：暴露 `.true` 与 `.false` 两个分支（`output_subset = \emptyset`）；
  * **声明 `with.output.result`（绑定变量 $v_{\text{res}}$）时**：校验 $\Sigma(v_{\text{res}}) \equiv \{ \texttt{type}: \texttt{boolean} \}$，无分支单出边（`output_subset = {v_res}`）。

---

### 2. `builtin:logic`（布尔逻辑运算算子）

```yaml
# 示例 1：逻辑与（支持作为分支路由或输出 boolean）
check_can_proceed:
  use: builtin:logic
  with:
    input:
      op: { value: "and" }          # "and" | "or" | "not"
      left: is_verified
      right: has_remaining_quota

# 示例 2：逻辑非（op 为 "not" 时仅需传入 source）
invert_flag:
  use: builtin:logic
  with:
    input:
      op: { value: "not" }
      source: is_dry_run
    output:
      result: should_commit
```

* **入参校验**：
  * `op`（必填）：字符串常量字面量 `{ value: "<op>" }`，取值属于 $\{\texttt{"and"}, \texttt{"or"}, \texttt{"not"}\}$；
  * 当 `op` $\in \{\texttt{"and"}, \texttt{"or"}\}$ 时：必填 `left` 与 `right`（禁止出现 `source`），两者类型均须为非空 `type: boolean`；
  * 当 `op` $= \texttt{"not"}$ 时：必填 `source`（禁止出现 `left`、`right`），类型须为非空 `type: boolean`。
* **出参与控制分支**：
  * 与 `builtin:compare` 一致：省略 `with.output` 时直接暴露 `.true` 与 `.false` 分支；声明 `with.output.result`（绑定 $v_{\text{res}}$）时校验 $\Sigma(v_{\text{res}}) \equiv \{ \texttt{type}: \texttt{boolean} \}$，无分支单出边。

---

### 3. `builtin:math`（安全基础算术算子）

为保持“所有 `builtin:*` 算子永不抛出可恢复运行时异常”的核心不变式，`builtin:math` 将加减乘与可能发生除零的除法/取模在静态分支契约上显式区分：

```yaml
# 示例 1：计数器自增（add / sub / mul 永不失败，无分支单出边）
increment_step:
  use: builtin:math
  with:
    input:
      op: { value: "add" }          # "add" | "sub" | "mul" | "div" | "mod"
      left: step_index
      right: 1
    output:
      result: step_index

# 示例 2：带兜底默认值的安全除法（传入 default，除数为 0 时返回 0.0，无分支单出边）
compute_avg_score:
  use: builtin:math
  with:
    input:
      op: { value: "div" }
      left: total_score
      right: item_count
      default: 0.0
    output:
      result: avg_score
```

* **入参校验**：
  * `op`（必填）：字符串常量字面量 `{ value: "<op>" }`，取值属于 $\{\texttt{"add"}, \texttt{"sub"}, \texttt{"mul"}, \texttt{"div"}, \texttt{"mod"}\}$；
  * `left`、`right`（必填）：设其类型为 $T_L, T_R$，必须满足 $\text{Nullable}(T_L) = \texttt{false} \;\land\; \text{Nullable}(T_R) = \texttt{false}$：
    * 当 `op` $\in \{\texttt{"add"}, \texttt{"sub"}, \texttt{"mul"}, \texttt{"div"}\}$ 时：$\text{BaseType}(T_L), \text{BaseType}(T_R) \in \{\texttt{integer}, \texttt{number}\}$；
    * 当 `op` $= \texttt{"mod"}$ 时：要求两侧均为整数 $\text{BaseType}(T_L) = \text{BaseType}(T_R) = \texttt{integer}$。
  * **结果数值类型 $T_{\text{math}}$**：
    * 当 `op` $\in \{\texttt{"add"}, \texttt{"sub"}, \texttt{"mul"}, \texttt{"mod"}\}$ 且 $\text{BaseType}(T_L) = \text{BaseType}(T_R) = \texttt{integer}$ 时，$T_{\text{math}} = \{ \texttt{type}: \texttt{integer} \}$；
    * 当 `op` $= \texttt{"div"}$（浮点真除法）或任一操作数为 `number` 时，$T_{\text{math}} = \{ \texttt{type}: \texttt{number} \}$。
  * `default`（可选，**仅当 `op` $\in \{\texttt{"div"}, \texttt{"mod"}\}$ 时允许声明**）：若声明，其类型须满足 $T_{\text{def}} <: \Sigma(v_{\text{res}})$（或字面量 $L_{\text{def}} \models \Sigma(v_{\text{res}})$）。
* **出参校验与静态控制分支**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：须满足 $T_{\text{math}} <: \Sigma(v_{\text{res}})$。
  * **当 `op` $\in \{\texttt{"add"}, \texttt{"sub"}, \texttt{"mul"}\}$ 或声明了 `default` 时**：无分支单出边（`output_subset = {v_res}`）；
  * **当 `op` $\in \{\texttt{"div"}, \texttt{"mod"}\}$ 且未声明 `default` 时**：
    * 要求 $v_{\text{res}} \neq \text{RootVar}(\text{left}) \;\land\; v_{\text{res}} \neq \text{RootVar}(\text{right})$；
    * 暴露 `.some`（除数非零且结果为有限数，`output_subset = {v_res}`）与 `.none`（除数为 `0`，`output_subset = \emptyset`，$\text{KillVars} = \{v_{\text{res}}\}$ 并置 $\rho(v_{\text{res}}) \leftarrow \text{Unbound}$）。

---

### 4. `builtin:length`（集合与字符串长度测量算子）

```yaml
get_batch_size:
  use: builtin:length
  with:
    input:
      source: candidate_items       # 非空 Array<T>、OrderedMap<string, V> 或 string
    output:
      result: total_count           # integer
```

* **入参校验**：
  * `source`（必填）：设 $T_{\text{src}} = \text{TypeOf}(\Sigma, \text{with.input.source})$。必须满足 $\text{Nullable}(T_{\text{src}}) = \texttt{false}$，且 $T_{\text{src}}$ 为 `Array<T>`、`OrderedMap<string, V>` 或 `type: string`（禁止对固定属性结构体 `TypedDict`、`integer`、`number`、`boolean` 求长度）。
* **出参校验与运行期语义**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：须满足 $\{ \texttt{type}: \texttt{integer} \} <: \Sigma(v_{\text{res}})$；
  * 无分支单出边（`output_subset = {v_res}`）；
  * 运行期返回列表元素个数、有序映射键值对个数或字符串 Unicode 字符（Code Points）长度。

---

### 5. `builtin:array_get`（列表按索引安全读取算子）

与 `builtin:map_get` 保持完全对称的设计，并支持负数索引（`-1` 表示最后一个元素）：

```yaml
# 形态 A：不传 default，暴露 .some（命中）与 .none（下标越界）分支
read_first_item:
  use: builtin:array_get
  with:
    input:
      source: ranked_candidates
      index: 0                      # 支持非空 integer 变量、路径或整数字面量（负数代表倒数）
    output:
      value: top_candidate

# 形态 B：传入 default，越界时自动回退至 default，收敛为无分支单出边
read_with_default:
  use: builtin:array_get
  with:
    input:
      source: ranked_candidates
      index: current_idx
      default: fallback_candidate
    output:
      value: selected_candidate
```

* **入参校验**：
  * `source`（必填）：必须为非空列表 $T_{\text{src}} = \{ \texttt{type}: \texttt{array}, \texttt{items}: T \}$（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）；
  * `index`（必填）：必须为非空 `type: integer`（变量、静态字段路径或整数字面量）；
  * `default`（可选）：若声明，引用表达式须满足 $\text{TypeOf}(\Sigma, E_{\text{def}}) <: \Sigma(v_{\text{out}})$，字面量须满足 $L_{\text{def}} \models \Sigma(v_{\text{out}})$。
* **出参校验与反定值（Kill）契约**：
  * `value`（必填，绑定顶层变量 $v_{\text{out}}$）：须满足 $T <: \Sigma(v_{\text{out}})$；
  * **未声明 `default` 时**：
    * 根变量异名约束：$v_{\text{out}} \neq \text{RootVar}(\text{with.input.source})$，且当 `index` 为引用表达式时 $v_{\text{out}} \neq \text{RootVar}(\text{with.input.index})$；
    * 暴露 `.some`（`output_subset = {v_out}`，$\text{KillVars} = \emptyset$）与 `.none`（`output_subset = \emptyset$，$\text{KillVars} = \{v_{\text{out}}\}$）；
  * **声明了 `default` 时**：无分支单出边（`output_subset = {v_out}`）。
* **运行期求值语义**：
  * 设源列表长度为 $m = |A|$，输入索引为 $idx \in \mathbb{Z}$。令归一化下标 $j = idx \text{ (若 } idx \ge 0 \text{)}$ 或 $j = m + idx \text{ (若 } idx < 0 \text{)}$；
  * 若 $0 \le j < m$，则命中元素 $A[j]$（写入 $\rho(v_{\text{out}}) \leftarrow A[j]$）；
  * 若 $j < 0$ 或 $j \ge m$（含空列表 $m = 0$）：未声明 `default` 时将 $\rho(v_{\text{out}}) \leftarrow \text{Unbound}$ 并转入 `.none` 出边；声明了 `default` 时将 $\rho(v_{\text{out}})$ 绑定为 `default` 求值结果并沿唯一出边前进。

---

### 6. `builtin:array_concat`（同构列表不可变拼接算子）

```yaml
merge_batches:
  use: builtin:array_concat
  with:
    input:
      left: primary_results         # Array<T>
      right: supplementary_results  # Array<T>
    output:
      result: all_results           # Array<T>
```

* **入参校验**：
  * `left`、`right`（必填）：必须均为非空列表，设 $T_L = \{ \texttt{type}: \texttt{array}, \texttt{items}: T_1 \}$，$T_R = \{ \texttt{type}: \texttt{array}, \texttt{items}: T_2 \}$（$\text{Nullable}(T_L) = \text{Nullable}(T_R) = \texttt{false}$）。
* **出参校验与运行期语义**：
  * `result`（必填，绑定顶层变量 $v_{\text{res}}$）：设目标类型 $\Sigma(v_{\text{res}}) = \{ \texttt{type}: \texttt{array}, \texttt{items}: T_{\text{target}} \}$（$\text{Nullable}(\Sigma(v_{\text{res}})) = \texttt{false}$），必须同时满足 $T_L <: \Sigma(v_{\text{res}})$ 且 $T_R <: \Sigma(v_{\text{res}})$（即 $T_1 <: T_{\text{target}} \;\land\; T_2 <: T_{\text{target}}$）；
  * 无分支单出边（`output_subset = {v_res}`）；
  * 运行期构造并返回包含 `left` 全部元素后接 `right` 全部元素的新列表副本 $[a_0, \dots, a_{m-1}, b_0, \dots, b_{n-1}]$。

---

## 三、 `manual/spec/` 形式化规范逐文件修改方案

### 1. [`index.md`](../manual/spec/index.md)

* 将 `02. 静态类型与符号表` 中的“9 个 `builtin:*` 算子”更新为“15 个 `builtin:*` 算子”。

### 2. [`02-type-system-and-symbols/05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md)

* 在现有第 1 ~ 9 节算子之后，依次新增第 10 ~ 15 节（`builtin:compare`、`builtin:logic`、`builtin:math`、`builtin:length`、`builtin:array_get`、`builtin:array_concat`）的入参校验、出参校验与静态分支推导规则。

### 3. [`03-static-flow-analysis/01-cfg-topology-and-sese-validation.md`](../manual/spec/03-static-flow-analysis/01-cfg-topology-and-sese-validation.md) 与 [`03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)

* 在 [`01-cfg-topology-and-sese-validation.md`](../manual/spec/03-static-flow-analysis/01-cfg-topology-and-sese-validation.md) 第二.2 节（并发组成员单出口无分支限制）中，补充禁止在并发组成员中直接使用未声明 `with.output` 的 `builtin:compare` / `builtin:logic`、未传 `default` 的 `builtin:array_get` 以及未传 `default` 的 `builtin:math`（`div` / `mod`）；
* 在 [`03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md) 第二.3 节（反定值杀死集 `KillVars(N, b)`）中，将未声明 `default` 的 `builtin:array_get` 和未声明 `default` 的 `builtin:math`（`div` / `mod`）在 `.none` 分支上的输出变量并入 `KillVars(N, none)`。

### 4. [`04-runtime-execution-model/03-guardrails-and-step-accounting.md`](../manual/spec/04-runtime-execution-model/03-guardrails-and-step-accounting.md)

* 在第四节“内建算子（`builtin:*`）运行期求值语义”中，增补 `builtin:compare`、`builtin:logic`、`builtin:math`、`builtin:length`、`builtin:array_get`、`builtin:array_concat` 的运行期求值与反定值（`Unbound`）状态转移方程。
