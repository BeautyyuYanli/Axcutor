# RFC-0004: 确定性数据赋值、结构体组装与集合查询算子扩展（`assign` / `map_get` / `object_merge`）

* **状态**：Implemented
* **所属分类**：对标主流编程语言的原语与内建算子扩展（Language Expressiveness & Builtin Extensions）
* **核心解决的问题**：新增 `builtin:assign`（零开销变量拷贝/字段提取/常量定值）、`builtin:map_get`（修复 `OrderedMap` 只写不读问题）与 `builtin:object_merge`（结构体不可变组装与更新）。
* **关联规范模块**：
  * [`02-type-system-and-symbols/05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md)
  * [`01-grammar-and-ast/02-binding-and-projection-grammar.md`](../manual/spec/01-grammar-and-ast/02-binding-and-projection-grammar.md)

---

## 一、 问题背景与表达力瓶颈

对标主流编程语言的基础数据操作能力，当前 Axcutor 规范仅提供了 5 个内建算子（`unwrap`、`switch`、`array_shift`、`array_append`、`map_set`），在实际编写工作流时存在三个严重的**确定性数据操作断层**，迫使用户不得不为了极其简单的纯内存数据搬运而调用外部 `run` 执行器（如 LLM 或外部脚本）：

### 痛点 4.1：无法执行确定性变量拷贝、结构体字段提取或分支常量赋值
1. **循环工作变量无法从输入或结构体字段初始化**：
   假设顶层输入为结构体 `request: { items: Array<string> }`，用户希望用 `builtin:array_shift` 编写一个 `while` 循环逐个消费 `items`。
   * 在第一次迭代后，`builtin:array_shift` 的 `rest` 必须写回一个**顶层变量** `queue`（因为 `with.output.rest` 禁止写 `request.items`）；
   * 这意味着 `builtin:array_shift` 的 `source` 必须绑定顶层变量 `queue`；
   * 但在进入循环头之前，**没有任何办法把 `request.items` 赋值给顶层变量 `queue`**！因为 `graph.variables` 只允许写静态常量字面量（不能引用 `request.items`），而 `builtin:unwrap` 明确禁止传入非空类型（`Nullable(request.items) == false` 会直接报编译期错误）。
2. **分支合流处的纯常量/变量定值**：
   在 `if-else` 分支合流前，分支 A 通过业务节点计算出了 `report`，分支 B 仅需将 `report` 设为一个静态常量字符串 `"Skipped"` 或已有的 `cached_report` 变量以满足必达定值分析。当前 DSL 在分支 B 上必须为此专门启动一个 `run` 节点。
3. **提取结构体嵌套字段作为图的顶层 `output`**：
   当节点产出了非空结构体 `user_info`，而宿主图 `output` 需要返回其中的 `user_info.email` 时，无法在不调用 `run` 的情况下将 `user_info.email` 绑定到顶层输出变量。

---

### 痛点 4.2：`OrderedMap` 成为“只写不读”容器（缺失 `builtin:map_get`）
* [`01-typespec-well-formedness.md`](../manual/spec/02-type-system-and-symbols/01-typespec-well-formedness.md) 引入了强类型有序映射 `OrderedMap<string, V>`（`type: object` + `additionalProperties: V`），且提供了写入算子 `builtin:map_set`。
* 然而，[`04-static-field-projection-typing.md`](../manual/spec/02-type-system-and-symbols/04-static-field-projection-typing.md#L19) 明确禁止对 `OrderedMap` 使用静态字段路径 `.field` 投影，同时内建算子库中**没有提供 `builtin:map_get`**（也没有提供键存在性检查或删除算子）。
* 结果是：在 Axcutor 中创建的 `OrderedMap`（例如缓存字典 `result_cache` 或按 ID 索引的配置表），除了用 `for_each` 全表遍历外，**完全无法根据动态 `key` 查找对应的 `value`**！

---

### 痛点 4.3：结构体（`TypedDict`）支持静态解构投影，但无法确定性构造或更新字段
* Axcutor 为保证并发安全性，规定所有变量写入均为顶层替换（$k = 0$，禁止 `user.age = 20` 原地可变写入）。
* 但在不可变数据模型（如 Rust / TypeScript / Haskell）中，禁止原地字段可变的前提是提供**记录构造（Struct Construction）与不可变记录更新（Record Update / Spread：`{ ...user, age: new_age }`）**。
* 当前 DSL 可以用 `user.name`、`user.age` 把结构体拆开读取，却无法将几个顶层变量确定性地组装成一个 `TypedDict`，也无法在不调用外部 `run` 的情况下更新 `TypedDict` 中的某个字段。

---

## 二、 解决方案：新增三个正交内建算子

在保持 Axcutor 既有 `use: builtin:<op>` 语法不变的前提下，扩展以下三个零开销确定性算子：

### 方案 4.1：`builtin:assign`（确定性变量赋值、路径投影提取与常量绑定）

允许在单步内将一个或多个源表达式（顶层变量、`FieldPathExpr` 静态字段路径、标量常量或 `{ value: <literal> }` 复合字面量）绑定写入当前图的顶层变量。

#### 1. 语法形态
为了支持一次给一到多个变量赋值，推荐支持以下简洁形式（或标准 `source -> value` 单变量形式）：

* **标准单变量形式**（与现有 `builtin:*` 完全同构）：
  ```yaml
  init_queue:
    use: builtin:assign
    with:
      input:
        source: request.items        # 可为变量、FieldPathExpr 或常量字面量
      output:
        value: working_queue         # 写入顶层变量 working_queue
  ```
* **编译期泛型推导规则**：
  * 若 `with.input.source` 为变量或静态字段路径表达式 $E$：令 $T = \text{TypeOf}(\Sigma, E)$，则推导输出变量类型 $\Sigma(v_{\text{out}}) = T$；
  * 若 `with.input.source` 为常量字面量 $L$：
    * 若 $v_{\text{out}} \in \text{dom}(\Sigma)$（已在显式契约中获得目标类型 $T$）：校验 $L \models T$；
    * 若 $v_{\text{out}} \notin \text{dom}(\Sigma)$：要求 $L$ 为非空基础标量字面量，推导为基础标量类型。
  * **控制流与定值**：无分支单出边（`output_subset = {v_out}`）。

---

### 方案 4.2：`builtin:map_get` 与 `builtin:map_delete`（补齐 `OrderedMap` 字典读写闭环）

#### 1. `builtin:map_get`（按动态键查找有序映射）
设计上与 `builtin:unwrap` 保持完全一致的控制流与拆包体验：

```yaml
# 形态 A：不传 default，暴露 .some 与 .none 分支
lookup_cache:
  use: builtin:map_get
  with:
    input:
      source: result_cache           # OrderedMap<string, V>
      key: current_query_id          # string（变量、路径或 { value: "k" }）
    output:
      value: cached_answer           # 仅在 .some 分支下获得定值（类型为 V）

# 形态 B：传入 default，收敛为无分支单出边
lookup_with_fallback:
  use: builtin:map_get
  with:
    input:
      source: score_map              # OrderedMap<string, number>
      key: module_name
      default: 0.0                   # 键不存在时使用兜底值 0.0
    output:
      value: module_score            # 单出边必达定值（类型为 number）
```

* **入参校验**：
  * `source`（必填）：必须为非空有序映射 $T_{\text{src}} = \{ \texttt{type}: \texttt{object}, \texttt{additionalProperties}: T_V \}$（$\text{Nullable}(T_{\text{src}}) = \texttt{false}$）；
  * `key`（必填）：必须为非空 `type: string`；
  * `default`（可选）：若声明，引用表达式须满足 $\text{TypeOf}(\Sigma, E_{\text{def}}) \equiv T_V$，字面量须满足 $L_{\text{def}} \models T_V$。
* **出参推导与静态控制分支**：
  * `value`（必填，绑定顶层变量 $v_{\text{out}}$）：推导类型 $\Sigma(v_{\text{out}}) = T_V$；
  * 未声明 `default` 时：暴露 `.some`（`output_subset = {v_out}`）与 `.none`（`output_subset = \emptyset$，且从定值集 Kill $v_{\text{out}}$）；
  * 声明了 `default` 时：无分支单出边（`output_subset = {v_out}`）。

#### 2. `builtin:map_delete`（可选，按键移除并返回新映射）
* **入参**：`source: OrderedMap<string, T_V>`，`key: string`；
* **出参**：`result: OrderedMap<string, T_V>`（无分支单出边，若 `key` 不存在则原样返回副本）。

---

### 方案 4.3：`builtin:object_merge`（结构体 `TypedDict` 构造与不可变字段更新）

为了在不破坏“顶层变量原子写回”的前提下支持构造 `TypedDict` 或更新 `TypedDict` 的部分字段，新增 `builtin:object_merge`（或 `builtin:struct`）：

```yaml
# 示例 1：基于已有结构体 user_profile 不可变更新 status 与 updated_at 字段
update_user_status:
  use: builtin:object_merge
  with:
    input:
      base: user_profile             # 可选：基础 TypedDict 变量或路径
      status: { value: "verified" }  # 覆盖或新增字段 status
      updated_at: current_timestamp  # 用变量覆盖字段 updated_at
    output:
      result: user_profile           # 写回顶层变量 user_profile

# 示例 2：不传 base，直接从多个变量组装出新的 TypedDict（目标类型可由下游契约或推导确立）
build_summary_record:
  use: builtin:object_merge
  with:
    input:
      doc_id: current_doc.id
      score: final_score
      passed: true
    output:
      result: summary_record
```

* **编译期静态类型规则**：
  * 除保留入参键 `base`（可选，若提供则必须为非空 `TypedDict`）外，`with.input` 中的其余每个键 $f_i$ 代表目标结构体的字段名；
  * 若存在 `base`（设类型为 $T_{\text{base}}$）：每个覆盖字段 $f_i$ 必须满足 $f_i \in \text{dom}(T_{\text{base}}.\text{properties})$，且绑定值类型兼容于 $T_{\text{base}}.\text{properties}[f_i]$，推导 `result` 类型为 $T_{\text{base}}$；
  * 若未提供 `base`：若目标变量 $v_{\text{res}}$ 已在符号表 $\Sigma$ 中有显式 `TypedDict` 类型 $T_{\text{target}}$，则校验字段集合满足 $\text{set}(T_{\text{target}}.\text{required}) \subseteq \{f_1, \dots, f_m\} \subseteq \text{dom}(T_{\text{target}}.\text{properties})$ 且各字段类型兼容；若 $v_{\text{res}}$ 尚无显式类型，则直接根据各字段推导构造闭合非空 `TypedDict`（`required` 为全部字段）。
