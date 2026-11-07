# 运行时内建节点字典（`builtin:*`）

Axcutor 运行时提供 10 个零开销、常数时间、确定性的泛型算子，统一通过 **`use: builtin:<op>`** 配合 **`with`** 调用。

---

## 一、 通用调用约定、运行期语义与使用限制

1. **显式符号表与静态泛型检查**：所有内建节点的输出目标变量均须已在当前图符号表 $\Sigma_G$ 中显式声明 `TypeSpec`（如在宿主 `output`、`graph.variables` 或其他未挂载 `for_each` 的 `run` / `graph` 节点的 `output` 中声明）；编译器在节点级静态类型检查阶段校验 `with.input` 与 `with.output` 绑定变量的类型完全匹配。
2. **必填参数显式声明**：与 `use: workflow:<path>` 一致，`use: builtin:<op>` 的所有必填 `with.input` 与 `with.output` 参数均必须显式写出，不支持隐式同名省略。
3. **运行期计步与纯函数式不可变更新**：
   * **计步规则**：每次激活 `use: builtin:<op>` 节点同样计 **`1` 步**（$\text{steps}(F_G) \leftarrow \text{steps}(F_G) + 1$），确保由纯内建算子构成的环路同样受 `graph.max_steps` 护栏保护。
   * **不可变值语义（Copy-on-Write）**：`builtin:array_shift`、`builtin:array_append`、`builtin:array_flatten`、`builtin:map_set`、`builtin:map_delete` 与 `builtin:object_merge` 始终构造并返回新的集合或结构体副本，绝不原地修改输入快照中的旧对象。
4. **禁用的节点修饰符**：所有 `builtin:*` 节点**禁止声明 `for_each`、`timeout_ms`、`retry` 或 `catch`**。
5. **并发组顶层限制**：带有控制分支的内建算子（`builtin:switch`、未传 `default` 的 `builtin:unwrap`、未传 `default` 的 `builtin:map_get`、`builtin:array_shift`）不可直接作为并发组 `[worker_a, worker_b]` 的顶层成员（若需在并发分支内部使用，应封装在内联 `graph` 子图中）；无分支算子（`builtin:assign`、传入 `default` 的 `builtin:unwrap`、`builtin:array_append`、`builtin:array_flatten`、`builtin:map_set`、传入 `default` 的 `builtin:map_get`、`builtin:map_delete`、`builtin:object_merge`）可用于任意无分支节点位置。

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
| **[`builtin:assign`](./assign.md)** | 将顶层变量、结构体静态字段路径（`FieldPathExpr`）或常量字面量绑定写入顶层变量 | `source`（变量、字段路径或常量字面量，必填） | `value`（已声明类型的目标顶层变量，必填） | 无分支单出边（定值 `value`） |
| **[`builtin:unwrap`](./unwrap.md)** | 将可空类型 `[T, "null"]`（含 `TypedDict` 非 `required` 可选叶子字段投影）按分支拆包为非空 `T`，或用 `default` 合并为非空 `T` | `source` (`[T, "null"]`, 必填)<br>`default` (`T`, 可选) | `value` (`T`, 必填，不可与 `source` 根变量同名) | • **省略 `default`**：暴露 **`.some`**（定值 `value`）与 **`.none`**（反定值移除 `value` 并置为未绑定）<br>• **传入 `default`**：无分支单出边（直接定值 `value`） |
| **[`builtin:switch`](./switch.md)** | 对非空 `boolean` 或带合法标识符 `enum` 的 `string` 执行确定性排他分支路由 | `source` (`boolean` 或带合法 `BranchId` `enum` 的 `string`, 必填) | *无（禁止声明 `with.output`）* | • `boolean`：暴露 **`.true`** / **`.false`**<br>• `enum`：暴露与各枚举值同名的 **`.<enum_val>`** |
| **[`builtin:array_shift`](./array-shift.md)** | 从列表头部弹出首元素与剩余切片副本（用于循环短路早退或动态工作队列） | `source` (`Array<T>`, 必填) | `item` (`T`, 必填，不可与 `rest` 同名)<br>`rest` (`Array<T>`, 必填，可与 `source` 同名覆盖) | 暴露 **`.some`**（定值 `item` 与 `rest`）与 **`.empty`**（反定值移除 `item` 并置为未绑定） |
| **[`builtin:array_append`](./array-append.md)** | 将单个元素追加到列表末尾并返回新列表副本 | `source` (`Array<T>`, 必填)<br>`item` (`T`, 必填) | `result` (`Array<T>`, 必填，可与 `source` 同名更新) | 无分支单出边（定值 `result`） |
| **[`builtin:array_flatten`](./array-flatten.md)** | 将非空二维列表 `Array<Array<W>>` 按序首尾拼接展平为一维列表 `Array<W>` | `source` (`Array<Array<W>>`, 必填) | `result` (`Array<W>`, 必填) | 无分支单出边（定值 `result`） |
| **[`builtin:map_set`](./map-set.md)** | 向有序映射插入或更新键值对并返回新副本（已有键保持原位更新，新键追加在末尾） | `source` (`OrderedMap<string, TV>`, 必填)<br>`key` (`string`, 必填)<br>`value` (`TV`, 必填) | `result` (`OrderedMap<string, TV>`, 必填，可与 `source` 同名更新) | 无分支单出边（定值 `result`） |
| **[`builtin:map_get`](./map-get.md)** | 按动态字符串键在有序映射中查找对应值，或用 `default` 回退默认值 | `source` (`OrderedMap<string, TV>`, 必填)<br>`key` (`string`, 必填)<br>`default` (`TV`, 可选) | `value` (`TV`, 必填，不可与 `source` 根变量同名) | • **省略 `default`**：暴露 **`.some`**（定值 `value`）与 **`.none`**（反定值移除 `value` 并置为未绑定）<br>• **传入 `default`**：无分支单出边（直接定值 `value`） |
| **[`builtin:map_delete`](./map-delete.md)** | 按动态字符串键从有序映射中移除条目并返回保持原键序的新副本 | `source` (`OrderedMap<string, TV>`, 必填)<br>`key` (`string`, 必填) | `result` (`OrderedMap<string, TV>`, 必填，可与 `source` 同名更新) | 无分支单出边（定值 `result`） |
| **[`builtin:object_merge`](./object-merge.md)** | 构造新的强类型结构体（`TypedDict`）或基于已有结构体 `base` 不可变覆盖更新字段 | `base` (`TypedDict`, 可选)<br>`<field_1> ... <field_m>`（目标结构体字段绑定，至少 1 个） | `result` (`TypedDict`, 必填，传入 `base` 时可与 `base` 同名更新) | 无分支单出边（定值 `result`） |
