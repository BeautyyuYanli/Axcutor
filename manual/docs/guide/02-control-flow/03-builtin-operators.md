# 03. Builtins：确定性内建算子与数据操作

在工作流编排中，大量的胶水逻辑（如从结构体提取嵌套字段、计算重试次数与均值、按下标或键查找容器、向列表追加元素或组装汇总结构体）完全是确定性的内存级操作。

Axcutor 提供了 **`use: builtin:<op>`** 原生算子体系，以**零外部执行器开销、常数时间复杂度、写时复制（Copy-on-Write）不可变更新**的方式在运行时内部直接完成数据转换。合理使用内建算子可以避免为简单的字段提取、算术运算或集合组装去启动昂贵的 `run` 业务执行器。

> 关于条件分流与可空拆包类内建算子（[`builtin:switch`](../../reference/builtins/switch.md)、[`builtin:compare`](../../reference/builtins/compare.md)、[`builtin:logic`](../../reference/builtins/logic.md)、[`builtin:unwrap`](../../reference/builtins/unwrap.md)），请参阅 [01. Branches：排他条件分支与按需产出](./01-branches.md)；完整的 16 个内建算子参数表可查阅 [内建节点字典](../../reference/builtins/index.md)。

---

## 一、 内建算子的调用模型与不可变更新语义

所有内建算子共享统一的调用语法、静态类型校验规则与运行期内存模型：

### 1. 统一调用形式（`use: builtin:<op>` + `with.input` / `with.output`）

内建算子与外部子工作流保持一致的 `use` + `with` 调用形态：
* **`with.input`（显式输入绑定）**：可绑定当前图中已定值的**顶层变量**（如 `source: scores`）、非空 `TypedDict` 的**静态字段路径**（如 `source: request.user.id`），或**常量字面量**（数值、布尔、`null` 可直接书写，字符串、列表、字典等字面量使用 `{ value: <literal> }` 包装以区分变量名，如 `op: { value: "add" }`）。
* **`with.output`（顶层输出绑定）**：必须绑定到当前图内的**顶层变量名**（不支持直接对结构体内部路径 `user.age` 原地赋值；更新结构体字段应使用 [`builtin:object_merge`](../../reference/builtins/object-merge.md)）。

### 2. 输出目标变量须预先在图符号表中声明 `TypeSpec`

内建算子大多为编译期泛型算子，不在节点内部重复编写冗长的 JSON Schema，而是依托所在图的符号表进行静态类型推导与单向子类型兼容性（`<:`）校验。因此，`with.output` 绑定的目标变量必须在以下任一位置预先声明明确的类型契约（`TypeSpec`）：
1. 宿主根工作流或内联 `graph` 节点的 `output` 契约；
2. 当前图的局部状态变量表 `graph.variables`；
3. 同一图内其他未挂载 `for_each` 的 `run` 或内联 `graph` 节点的 `output` 契约。

### 3. 纯函数式写时复制（Copy-on-Write）与同名变量滚动更新

所有涉及列表（`Array<T>`）、有序映射（`OrderedMap<string, V>`）和结构体（`TypedDict`）的内建算子均遵循**纯函数式不可变语义**：算子始终在内存中构造并返回新的集合或结构体副本，**绝不原地修改（Mutate）**输入变量所引用的旧快照。

在无 `.none` 分支的更新算子（如 `builtin:math`、`builtin:array_append`、`builtin:map_set`、`builtin:object_merge`）中，允许将 `with.output.result` 绑定回与 `with.input` 相同的顶层变量名（如 `source: logs` → `result: logs`），从而安全地将变量名重绑定到新副本。

### 4. `.some` / `.none` 双分支模式、`default` 回退模式与反定值保护

可能出现“未命中/越界/除零/空值”的算子（如 [`builtin:unwrap`](../../reference/builtins/unwrap.md)、[`builtin:math`](../../reference/builtins/math.md) 的 `div`/`mod`、[`builtin:array_get`](../../reference/builtins/array-get.md)、[`builtin:map_get`](../../reference/builtins/map-get.md)）统一支持两种控制流模式：

* **条件双分支模式（省略 `default`）**：节点暴露 **`.some`** 与 **`.none`** 两个排他分支：
  * **`.some` 分支**：操作成功或查找命中，定值输出变量；
  * **`.none` 分支（反定值 Kill 保护）**：操作未命中或除数为零，不产出输出变量，且编译器与运行时会强制将该目标变量重置为**未定值（Unbound）**状态，防止下游误读上一轮循环或前序路径残留的脏值（因此省略 `default` 时，输出变量禁止与输入源根变量同名）。
* **默认值回退模式（传入 `default`）**：收敛为**无分支单出边（`node -> target`）**，未命中或除数为零时自动回退为 `default` 值，下游路径始终保证已定值。

---

## 二、 标量赋值、字段提取与安全算术（`assign` / `math` / `length`）

### 1. [`builtin:assign`](../../reference/builtins/assign.md)：字段提取、常量绑定与子类型拓宽

`builtin:assign` 将顶层变量、结构体静态字段路径（`FieldPathExpr`）或常量字面量直接写入目标顶层变量 `value`，常用于：
* 从输入结构体提取嵌套字段赋给宿主 `output` 或初始化循环队列；
* 在跳过分支或异常恢复分支上为汇合变量绑定静态常量；
* 执行安全的单向子类型拓宽（如将非空 `T` 赋给可空 `[T, "null"]`、将 `integer` 赋给 `number`）。

```yaml
graph:
  variables:
    working_queue:
      type: array
      items: { type: string }
    optional_contact:
      type: [string, "null"]

  nodes:
    # 1. 提取结构体嵌套字段：将 request.payload.items 拷贝至顶层变量 working_queue
    extract_queue:
      use: builtin:assign
      with:
        input:
          source: request.payload.items
        output:
          value: working_queue

    # 2. 常量绑定：直接将字面量字符串 "Skipped" 写入宿主输出变量 audit_status
    bind_skipped_status:
      use: builtin:assign
      with:
        input:
          source: { value: "Skipped" }
        output:
          value: audit_status

    # 3. 子类型拓宽：将非空 string 字段 user.email 安全赋给可空变量 optional_contact ([string, "null"])
    widen_to_nullable:
      use: builtin:assign
      with:
        input:
          source: user.email
        output:
          value: optional_contact
```

### 2. [`builtin:math`](../../reference/builtins/math.md) 与 [`builtin:length`](../../reference/builtins/length.md)：除零安全算术与长度测量

* **[`builtin:math`](../../reference/builtins/math.md)**：支持 `add`、`sub`、`mul`、`div`、`mod` 五种二元运算。其中 `add` / `sub` / `mul` 始终为无分支单出边；`div` 与 `mod`（`mod` 要求左右操作数均为 `integer`）在传入 `default` 时为无分支单出边，省略 `default` 时暴露 `.some` 与除零保护分支 `.none`。
* **[`builtin:length`](../../reference/builtins/length.md)**：非破坏性地测量非空列表 `Array<T>`、有序映射 `OrderedMap<string, V>` 或字符串 `string` 的元素/字符个数，输出非负 `integer`。

```yaml
graph:
  variables:
    item_count: { type: integer }
    total_with_tax: { type: number }
    avg_price: { type: number }
    unit_share: { type: number }

  nodes:
    # 1. 测量列表 items 的长度，写入 integer 变量 item_count
    measure_items:
      use: builtin:length
      with:
        input:
          source: order.items
        output:
          result: item_count

    # 2. 乘法运算（mul）：永不失败，无分支单出边
    calc_taxed_total:
      use: builtin:math
      with:
        input:
          op: { value: "mul" }
          left: order.subtotal
          right: 1.08
        output:
          result: total_with_tax

    # 3. 带 default 的安全除法（div）：当 item_count == 0 时自动回退为 0.0，无分支单出边
    calc_avg_safe:
      use: builtin:math
      with:
        input:
          op: { value: "div" }
          left: total_with_tax
          right: item_count
          default: 0.0
        output:
          result: avg_price

    # 4. 不传 default 的条件除法（div）：暴露 .some（除数非 0）与 .none（除数为 0）分支
    calc_unit_share:
      use: builtin:math
      with:
        input:
          op: { value: "div" }
          left: total_with_tax
          right: partner_count
        output:
          result: unit_share

  edges:
    - start -> measure_items
    - measure_items -> calc_taxed_total
    - calc_taxed_total -> calc_avg_safe
    - calc_avg_safe -> calc_unit_share
    - calc_unit_share.some -> record_settlement
    - calc_unit_share.none -> handle_zero_partners
```

---

## 三、 列表随机读取、拼接与展平（`array_get` / `array_concat` / `array_flatten`）

用于队列头部弹出与末尾追加的 [`builtin:array_shift`](../../reference/builtins/array-shift.md) 与 [`builtin:array_append`](../../reference/builtins/array-append.md) 已在上一节 [02. 成环回边：动态条件循环](./02-cycles-and-back-edges.md) 中介绍。除此之外，Axcutor 还提供按索引随机读取、同构列表拼接与二维列表展平算子：

### 1. [`builtin:array_get`](../../reference/builtins/array-get.md)：正向与负向下标安全读取

按整数 `index` 非破坏性地读取 `Array<T>` 中的元素。`index` 支持从 `0` 起算的**正向索引**（`0` 表示首元素）以及从末尾倒数起算的**负向索引**（`-1` 表示最后一个元素，`-2` 表示倒数第二个元素）：
* **省略 `default`**：暴露 `.some`（索引在有效范围内）与 `.none`（索引越界或列表为空）两个分支；
* **传入 `default`**：无分支单出边，越界时自动返回 `default` 值。

### 2. [`builtin:array_concat`](../../reference/builtins/array-concat.md) 与 [`builtin:array_flatten`](../../reference/builtins/array-flatten.md)：列表拼接与二维展平

* **[`builtin:array_concat`](../../reference/builtins/array-concat.md)**：将两个同构列表 `left` 与 `right` 按顺序首尾拼接，返回新列表副本；
* **[`builtin:array_flatten`](../../reference/builtins/array-flatten.md)**：将非空二维列表 `Array<Array<W>>`（例如 `for_each` 节点单次迭代产出列表、经一阶集合提升后形成的二维列表）单层保序展平为一维列表 `Array<W>`。

```yaml
graph:
  variables:
    latest_entry: { type: string }
    first_entry: { type: string }
    combined_batches:
      type: array
      items:
        type: array
        items: { type: string }
    flat_items:
      type: array
      items: { type: string }

  nodes:
    # 1. 将两个二维批次列表 primary_batches 与 extra_batches 首尾拼接
    concat_batches:
      use: builtin:array_concat
      with:
        input:
          left: primary_batches
          right: extra_batches
        output:
          result: combined_batches

    # 2. 将二维列表 combined_batches (Array<Array<string>>) 单层保序展平为一维列表 flat_items (Array<string>)
    flatten_batches:
      use: builtin:array_flatten
      with:
        input:
          source: combined_batches
        output:
          result: flat_items

    # 3. 传入 default 按正向索引 0 读取首元素（若为空列表则回退为 "N/A"，无分支单出边）
    get_first_with_default:
      use: builtin:array_get
      with:
        input:
          source: flat_items
          index: 0
          default: { value: "N/A" }
        output:
          value: first_entry

    # 4. 省略 default 按负向索引 -1 读取末尾元素（暴露 .some 与越界分支 .none）
    get_last_conditional:
      use: builtin:array_get
      with:
        input:
          source: flat_items
          index: -1
        output:
          value: latest_entry

  edges:
    - start -> concat_batches
    - concat_batches -> flatten_batches
    - flatten_batches -> get_first_with_default
    - get_first_with_default -> get_last_conditional
    - get_last_conditional.some -> process_latest
    - get_last_conditional.none -> handle_empty_list
```

---

## 四、 有序映射读写与结构体不可变组装（`map_get` / `map_set` / `map_delete` / `object_merge`）

Axcutor 严格区分两类 `type: object` 数据结构：
* **动态键有序映射（`OrderedMap<string, V>`）**：声明为 `type: object` 且配置 `additionalProperties: <TypeSpec>`，键集合在运行期动态增删，通过 `map_get` / `map_set` / `map_delete` 操作；
* **固定字段结构体（`TypedDict`）**：声明为 `type: object` 且配置固定的 `properties`，通过点号路径 `a.b` 静态读取，并通过 `object_merge` 不可变构造或更新。

### 1. 有序映射操作：[`builtin:map_get`](../../reference/builtins/map-get.md)、[`builtin:map_set`](../../reference/builtins/map-set.md) 与 [`builtin:map_delete`](../../reference/builtins/map-delete.md)

* **`builtin:map_get`**：按动态字符串 `key` 查找值。省略 `default` 时暴露 `.some`（键存在）与 `.none`（键不存在并触发反定值保护）双分支；传入 `default` 时收敛为无分支单出边。
* **`builtin:map_set`**：以写时复制方式向 `OrderedMap` 写入 `key: value` 并保持插入序——若 `key` 已存在则**原位更新**其值并保持原有键序不变；若 `key` 不存在则**追加在映射末尾**。
* **`builtin:map_delete`**：以写时复制方式移除指定 `key` 并保持其余键的相对插入顺序不变（若 `key` 不存在则原样返回浅拷贝副本，不报错）。

```yaml
graph:
  variables:
    # 声明 OrderedMap<string, integer>
    quota_map:
      type: object
      additionalProperties: { type: integer }
    user_quota:
      type: integer

  nodes:
    # 1. 按动态键 request.tenant_id 查找配额，未命中时回退为默认配额 100
    read_tenant_quota:
      use: builtin:map_get
      with:
        input:
          source: quota_map
          key: request.tenant_id
          default: 100
        output:
          value: user_quota

    # 2. 向有序映射写入/更新键 "vip_override": 500（保持插入序，写时复制写回 quota_map）
    upsert_vip_quota:
      use: builtin:map_set
      with:
        input:
          source: quota_map
          key: { value: "vip_override" }
          value: 500
        output:
          result: quota_map

    # 3. 从有序映射中安全移除已废弃的键 request.deprecated_key
    remove_deprecated_key:
      use: builtin:map_delete
      with:
        input:
          source: quota_map
          key: request.deprecated_key
        output:
          result: quota_map
```

### 2. 结构体不可变组装与覆盖：[`builtin:object_merge`](../../reference/builtins/object-merge.md)

`builtin:object_merge` 用于在禁止原地修改字段的前提下构造或更新强类型 `TypedDict` 结构体，支持两种模式：
* **从零组装结构体（省略 `base`）**：`with.input` 中列出的字段必须属于目标结构体 `properties`，且必须完整覆盖 `required` 中的所有必填字段；
* **基于 `base` 不可变覆盖字段（传入 `base`）**：先浅拷贝 `base` 结构体的全部属性，再用 `with.input` 中显式列出的字段覆盖对应属性，构造新结构体副本写回 `result`（允许 `result` 与 `base` 同名）。

```yaml
graph:
  variables:
    audit_record:
      type: object
      properties:
        order_id: { type: string }
        reviewer: { type: string }
        passed: { type: boolean }
        score: { type: number }
      required: [order_id, reviewer, passed, score]

  nodes:
    # 模式 A（省略 base）：从零组装完整的 TypedDict 结构体 audit_record
    build_initial_record:
      use: builtin:object_merge
      with:
        input:
          order_id: request.id
          reviewer: { value: "system_auto" }
          passed: false
          score: 60.0
        output:
          result: audit_record

    # 模式 B（传入 base）：基于已有 audit_record 不可变覆盖 passed 与 score 字段，并写回 audit_record
    promote_audit_record:
      use: builtin:object_merge
      with:
        input:
          base: audit_record
          passed: true
          score: 95.5
        output:
          result: audit_record
```

---

## 五、 完整示例：零 `run` 节点的纯确定性数据清洗与组装流水线

下面编写一个完整、合法的 Axcutor 工作流：在不调用任何外部 `run` 执行器的前提下，串联 [`builtin:unwrap`](../../reference/builtins/unwrap.md)、[`builtin:map_get`](../../reference/builtins/map-get.md)、[`builtin:math`](../../reference/builtins/math.md)、[`builtin:compare`](../../reference/builtins/compare.md)、[`builtin:array_append`](../../reference/builtins/array-append.md) 与 [`builtin:object_merge`](../../reference/builtins/object-merge.md)，完成可选配置解析、租户费率查找、账单金额计算、审计轨迹追加与最终结算单结构体的组装：

```yaml
# 1. 根节点输入契约：包含计量原始数据与租户费率有序映射表
input:
  usage_event:
    type: object
    properties:
      event_id: { type: string }
      tenant_id: { type: string }
      units: { type: integer }
      discount_override:
        type: [number, "null"]
      trace_logs:
        type: array
        items: { type: string }
    required: [event_id, tenant_id, units, discount_override, trace_logs]
  tier_rates:
    type: object
    additionalProperties: { type: number }

# 2. 根节点输出契约：强类型结算单结构体 invoice_summary
output:
  invoice_summary:
    type: object
    properties:
      event_id: { type: string }
      tenant_id: { type: string }
      unit_rate: { type: number }
      applied_discount: { type: number }
      gross_amount: { type: number }
      final_amount: { type: number }
      high_value_flag: { type: boolean }
      trace_logs:
        type: array
        items: { type: string }
    required:
      - event_id
      - tenant_id
      - unit_rate
      - applied_discount
      - gross_amount
      - final_amount
      - high_value_flag
      - trace_logs

graph:
  # 3. 声明图内中间变量 TypeSpec
  variables:
    applied_discount: { type: number }
    unit_rate: { type: number }
    gross_amount: { type: number }
    final_amount: { type: number }
    high_value_flag: { type: boolean }
    updated_logs:
      type: array
      items: { type: string }

  nodes:
    # 步骤 1：拆包可空折扣字段 usage_event.discount_override，若为 null 则回退为默认无折扣 1.0
    resolve_discount:
      use: builtin:unwrap
      with:
        input:
          source: usage_event.discount_override
          default: 1.0
        output:
          value: applied_discount

    # 步骤 2：在有序映射 tier_rates 中按动态键 usage_event.tenant_id 查找单价，未命中则回退为标准单价 0.5
    lookup_unit_rate:
      use: builtin:map_get
      with:
        input:
          source: tier_rates
          key: usage_event.tenant_id
          default: 0.5
        output:
          value: unit_rate

    # 步骤 3：计算折前总价 gross_amount = usage_event.units * unit_rate（integer 与 number 相乘产出 number）
    calc_gross_amount:
      use: builtin:math
      with:
        input:
          op: { value: "mul" }
          left: usage_event.units
          right: unit_rate
        output:
          result: gross_amount

    # 步骤 4：计算折后总价 final_amount = gross_amount * applied_discount
    calc_final_amount:
      use: builtin:math
      with:
        input:
          op: { value: "mul" }
          left: gross_amount
          right: applied_discount
        output:
          result: final_amount

    # 步骤 5：判断是否为高价值账单 (final_amount >= 1000.0)，将布尔结果写入 high_value_flag
    check_high_value:
      use: builtin:compare
      with:
        input:
          op: { value: "gte" }
          left: final_amount
          right: 1000.0
        output:
          result: high_value_flag

    # 步骤 6：以写时复制方式向 trace_logs 末尾追加结算完成标记，生成新列表 updated_logs
    append_audit_trace:
      use: builtin:array_append
      with:
        input:
          source: usage_event.trace_logs
          item: { value: "deterministic_billing_completed" }
        output:
          result: updated_logs

    # 步骤 7：使用 builtin:object_merge 从零组装强类型输出结构体 invoice_summary
    assemble_invoice:
      use: builtin:object_merge
      with:
        input:
          event_id: usage_event.event_id
          tenant_id: usage_event.tenant_id
          unit_rate: unit_rate
          applied_discount: applied_discount
          gross_amount: gross_amount
          final_amount: final_amount
          high_value_flag: high_value_flag
          trace_logs: updated_logs
        output:
          result: invoice_summary

  # 4. 纯确定性线性流水线控制流
  edges:
    - start -> resolve_discount
    - resolve_discount -> lookup_unit_rate
    - lookup_unit_rate -> calc_gross_amount
    - calc_gross_amount -> calc_final_amount
    - calc_final_amount -> check_high_value
    - check_high_value -> append_audit_trace
    - append_audit_trace -> assemble_invoice
    - assemble_invoice -> end
```

---

下一节：[01. 同构结构化并发：集合遍历（for_each）](../03-concurrency/01-for-each.md)。
