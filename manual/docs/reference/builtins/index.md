# 运行时内建节点字典（`builtin:*`）

Axcutor 运行时提供 16 个零开销、常数时间、确定性的泛型算子，统一通过 **`use: builtin:<op>`** 配合 **`with`** 调用。

---

## 一、 通用调用约定、运行期语义与使用限制

1. **显式符号表、泛型类型检查与子类型兼容（`<:`）**：所有内建节点的输出目标变量均须已在当前图符号表 $\Sigma_G$ 中显式声明 `TypeSpec`（如在宿主 `output`、`graph.variables` 或其他未挂载 `for_each` 的 `run` / `graph` 节点的 `output` 中声明）；编译器在节点级静态类型检查阶段校验 `with.input` 与 `with.output` 绑定变量满足单向静态子类型兼容（$T_{\text{actual}} <: T_{\text{expected}}$）。
2. **必填参数显式声明**：与 `use: workflow:<path>` 一致，`use: builtin:<op>` 的所有必填 `with.input` 与 `with.output` 参数均必须显式写出，不支持隐式同名省略（其中 `builtin:switch` 禁止声明 `with.output`；`builtin:compare` 与 `builtin:logic` 的 `with.output` 可选，省略时直接暴露 `.true` / `.false` 分支，显式声明 `result` 时为无分支单出边；其余 13 个算子的 `with.output` 必填）。
3. **运行期计步与纯函数式不可变更新**：
   * **计步规则**：每次激活 `use: builtin:<op>` 节点同样计 **`1` 步**（$\text{steps}(F_G) \leftarrow \text{steps}(F_G) + 1$），确保由纯内建算子构成的环路同样受 `graph.max_steps` 护栏保护。
   * **不可变值语义（Copy-on-Write）**：`builtin:array_shift`、`builtin:array_append`、`builtin:array_flatten`、`builtin:array_concat`、`builtin:map_set`、`builtin:map_delete` 与 `builtin:object_merge` 始终构造并返回新的集合或结构体副本，绝不原地修改输入快照中的旧对象。
4. **禁用的节点修饰符**：所有 `builtin:*` 节点**禁止声明 `for_each`、`timeout_ms`、`retry` 或 `catch`**。
5. **并发组顶层限制**：带有控制分支的内建算子（`builtin:switch`、省略 `with.output` 的 `builtin:compare` / `builtin:logic`、未传 `default` 的 `builtin:unwrap`、未传 `default` 的 `builtin:map_get`、未传 `default` 的 `builtin:array_get`、未传 `default` 的 `builtin:math`（`div` / `mod`）、`builtin:array_shift`）不可直接作为并发组 `[worker_a, worker_b]` 的顶层成员（若需在并发分支内部使用，应封装在内联 `graph` 子图中）；无分支算子（`builtin:assign`、声明了 `with.output` 的 `builtin:compare` / `builtin:logic`、传入 `default` 的 `builtin:unwrap`、`builtin:math`（`add` / `sub` / `mul` 或带 `default` 的 `div` / `mod`）、`builtin:length`、传入 `default` 的 `builtin:array_get`、`builtin:array_append`、`builtin:array_concat`、`builtin:array_flatten`、`builtin:map_set`、传入 `default` 的 `builtin:map_get`、`builtin:map_delete`、`builtin:object_merge`）可用于任意无分支节点位置。

```yaml
nodes:
  # 统一通过 use: builtin:<op> 与 with.input / with.output 调用
  resolve_operator:
    use: builtin:unwrap
    with:
      input:
        source: ticket.operator_override
        default: { value: "auto_bot" }
      output:
        value: assigned_operator
```

---

## 二、 内建算子契约对照总表

| 内建算子 | 核心用途 | `with.input` 参数 | `with.output` 参数 | 控制分支与出边 |
| :--- | :--- | :--- | :--- | :--- |
| **[`builtin:assign`](./assign.md)** | 将顶层变量、结构体静态字段路径（`FieldPathExpr`）或常量字面量绑定写入顶层变量（支持子类型提升与容器协变） | `source`（变量、字段路径或常量字面量，必填） | `value`（已声明父类型的目标顶层变量，必填） | 无分支单出边（定值 `value`） |
| **[`builtin:unwrap`](./unwrap.md)** | 将可空类型 `[T, "null"]`（含 `TypedDict` 非 `required` 可选叶子字段投影）按分支拆包为非空 `T_out`，或用 `default` 合并为非空 `T_out` | `source` (`[T, "null"]`, 必填)<br>`default` ($<: T_{\text{out}}$, 可选) | `value` ($T_{\text{out}}$，必填非空且满足 $T <: T_{\text{out}}$，不可与 `source` 根变量同名) | • **省略 `default`**：暴露 **`.some`**（定值 `value`）与 **`.none`**（反定值移除 `value` 并置为未绑定）<br>• **传入 `default`**：无分支单出边（直接定值 `value`） |
| **[`builtin:switch`](./switch.md)** | 对非空 `boolean` 或带合法标识符 `enum` 的 `string` 执行确定性排他分支路由 | `source` (`boolean` 或带合法 `BranchId` `enum` 的 `string`, 必填) | *无（禁止声明 `with.output`）* | • `boolean`：暴露 **`.true`** / **`.false`**<br>• `enum`：暴露与各枚举值同名的 **`.<enum_val>`** |
| **[`builtin:compare`](./compare.md)** | 对两个操作数执行相等性（`eq`, `ne`）或序关系（`lt`, `lte`, `gt`, `gte`）二元比较 | `op` (`"eq" \| "ne" \| "lt" \| "lte" \| "gt" \| "gte"`, 必填字面量)<br>`left`（左操作数，必填）<br>`right`（右操作数，必填） | `result` (`boolean`, 可选) | • **省略 `with.output`**：暴露 **`.true`** / **`.false`**<br>• **声明 `result`**：无分支单出边（定值 `result`） |
| **[`builtin:logic`](./logic.md)** | 对非空布尔值执行逻辑与（`and`）、逻辑或（`or`）或逻辑非（`not`）运算 | `op` (`"and" \| "or" \| "not"`, 必填字面量)<br>• `and`/`or` 时：`left`, `right` (`boolean`, 必填)<br>• `not` 时：`source` (`boolean`, 必填) | `result` (`boolean`, 可选) | • **省略 `with.output`**：暴露 **`.true`** / **`.false`**<br>• **声明 `result`**：无分支单出边（定值 `result`） |
| **[`builtin:math`](./math.md)** | 对非空数值执行二元算术运算（`add`, `sub`, `mul`, `div`, `mod`），除零安全 | `op` (`"add" \| "sub" \| "mul" \| "div" \| "mod"`, 必填字面量)<br>`left`, `right`（非空 `integer`/`number`，`mod` 仅限 `integer`，必填）<br>`default`（仅 `div`/`mod` 可选） | `result` ($T_{\text{res}}$，必填且 $T_{\text{math}} <: T_{\text{res}}$；无 `default` 的 `div`/`mod` 时不可与操作数根变量同名) | • **`add`/`sub`/`mul` 或传入 `default`**：无分支单出边（定值 `result`）<br>• **`div`/`mod` 且省略 `default`**：暴露 **`.some`**（定值 `result`）与 **`.none`**（除数为 `0`，反定值移除 `result`） |
| **[`builtin:length`](./length.md)** | 非破坏性地获取非空列表 `Array<T>`、有序映射 `OrderedMap<string, V>` 或字符串 `string` 的长度 | `source`（非空 `Array`、`OrderedMap` 或 `string`，必填） | `result` (`integer`, 必填) | 无分支单出边（定值 `result`） |
| **[`builtin:array_get`](./array-get.md)** | 按整数下标 `index`（支持负数倒数索引）非破坏性地读取列表元素，或用 `default` 回退默认值 | `source` (`Array<T>`, 必填)<br>`index` (`integer`, 必填，支持负数)<br>`default` ($<: T_{\text{out}}$, 可选) | `value` ($T_{\text{out}}$，必填且 $T <: T_{\text{out}}$；省略 `default` 时不可与 `source`/`index` 根变量同名) | • **省略 `default`**：暴露 **`.some`**（定值 `value`）与 **`.none`**（越界，反定值移除 `value`）<br>• **传入 `default`**：无分支单出边（直接定值 `value`） |
| **[`builtin:array_shift`](./array-shift.md)** | 从列表头部弹出首元素与剩余切片副本（用于循环短路早退或动态工作队列） | `source` (`Array<T>`, 必填) | `item` ($T_{\text{item}}$，必填且 $T <: T_{\text{item}}$，不可与 `rest` 同名)<br>`rest` ($T_{\text{rest}}$，必填且 `Array<T>` $<: T_{\text{rest}}$，可与 `source` 同名覆盖) | 暴露 **`.some`**（定值 `item` 与 `rest`）与 **`.empty`**（反定值移除 `item` 并置为未绑定） |
| **[`builtin:array_append`](./array-append.md)** | 将单个元素追加到列表末尾并返回新列表副本 | `source` ($<: \text{Array}\langle T_{\text{target}}\rangle$, 必填)<br>`item` ($<: T_{\text{target}}$, 必填) | `result` (`Array<T_target>`, 必填，可与 `source` 同名更新) | 无分支单出边（定值 `result`） |
| **[`builtin:array_concat`](./array-concat.md)** | 将两个同构列表 `left` 与 `right` 按顺序首尾拼接并返回新列表副本 | `left` ($<: \text{Array}\langle T_{\text{target}}\rangle$, 必填)<br>`right` ($<: \text{Array}\langle T_{\text{target}}\rangle$, 必填) | `result` (`Array<T_target>`, 必填，可与 `left` 或 `right` 同名更新) | 无分支单出边（定值 `result`） |
| **[`builtin:array_flatten`](./array-flatten.md)** | 将非空二维列表 `Array<Array<W>>` 按序首尾拼接展平为一维列表 | `source` (`Array<Array<W>>`, 必填) | `result` ($T_{\text{res}}$，必填且 `Array<W>` $<: T_{\text{res}}$) | 无分支单出边（定值 `result`） |
| **[`builtin:map_set`](./map-set.md)** | 向有序映射插入或更新键值对并返回新副本（已有键保持原位更新，新键追加在末尾） | `source` ($<: \text{OrderedMap}\langle\text{string}, T_{V,\text{target}}\rangle$, 必填)<br>`key` ($<: \text{string}$, 必填)<br>`value` ($<: T_{V,\text{target}}$, 必填) | `result` (`OrderedMap<string, TV_target>`, 必填，可与 `source` 同名更新) | 无分支单出边（定值 `result`） |
| **[`builtin:map_get`](./map-get.md)** | 按动态字符串键在有序映射中查找对应值，或用 `default` 回退默认值 | `source` (`OrderedMap<string, TV>`, 必填)<br>`key` ($<: \text{string}$, 必填)<br>`default` ($<: T_{\text{out}}$, 可选) | `value` ($T_{\text{out}}$，必填且 $T_V <: T_{\text{out}}$，不可与 `source` 根变量同名) | • **省略 `default`**：暴露 **`.some`**（定值 `value`）与 **`.none`**（反定值移除 `value` 并置为未绑定）<br>• **传入 `default`**：无分支单出边（直接定值 `value`） |
| **[`builtin:map_delete`](./map-delete.md)** | 按动态字符串键从有序映射中移除条目并返回保持原键序的新副本 | `source` (`OrderedMap<string, TV>`, 必填)<br>`key` ($<: \text{string}$, 必填) | `result` ($T_{\text{res}}$，必填且 $T_{\text{src}} <: T_{\text{res}}$，可与 `source` 同名更新) | 无分支单出边（定值 `result`） |
| **[`builtin:object_merge`](./object-merge.md)** | 构造新的强类型结构体（`TypedDict`）或基于已有结构体 `base` 不可变覆盖更新字段 | `base` (`TypedDict`, 可选)<br>`<field_1> ... <field_m>`（目标结构体字段绑定，至少 1 个，支持子类型传入） | `result` (`TypedDict`, 必填，传入 `base` 时可与 `base` 同名更新) | 无分支单出边（定值 `result`） |
