# 运行时内建节点字典（`builtin:*`）

Axcutor 运行时内置了 5 个零开销、常数时间、确定性的泛型算子，统一通过 **`use: builtin:<op>`** 配合 **`with`** 调用。

---

## 一、 通用调用约定与使用限制

1. **无需手写输出类型**：所有内建节点的输出变量类型均由静态检查器根据 `with.input` 绑定的源表达式自动推导。
2. **禁止挂载的修饰符**：所有 `builtin:*` 节点**禁止声明 `for_each`、`timeout_ms`、`retry` 或 `catch`**。
3. **并发组顶层限制**：带有控制分支的内建算子（`builtin:switch`、未传 `default` 的 `builtin:unwrap`、`builtin:array_shift`）不可直接放在并发组 `[worker_a, worker_b]` 顶层（如需在并发分支内部使用，请包裹在内联 `graph` 子图中）；无分支算子（传入 `default` 的 `builtin:unwrap`、`builtin:array_append`、`builtin:map_set`）可用于任意无分支节点位置。

---

## 二、 内建节点速查总表

| 内建节点 | 核心用途 | `with.input` 参数 | `with.output` 参数 | 控制分支与出边 |
| :--- | :--- | :--- | :--- | :--- |
| **[`builtin:unwrap`](./unwrap.md)** | 将可空类型 `[T, "null"]` 按分支拆包为非空 `T`，或用 `default` 合并为非空 `T` | `source` (`[T, "null"]`, 必填)<br>`default` (`T`, 可选) | `value` (`T`, 必填，不可与 `source` 根变量同名) | • **省略 `default`**：暴露 **`.some`**（定值 `value`）与 **`.none`**（不写入 `value`）<br>• **传入 `default`**：无分支单出边（直接定值 `value`） |
| **[`builtin:switch`](./switch.md)** | 对非空 `boolean` 或带 `enum` 的 `string` 做确定性排他分支路由 | `source` (`boolean` 或带 `enum` 的 `string`, 必填) | *无（禁止声明 `with.output`）* | • `boolean`：暴露 **`.true`** / **`.false`**<br>• `enum`：暴露与各枚举值同名的 **`.\<enum_val>`** |
| **[`builtin:array_shift`](./array-shift.md)** | 从列表头部弹出首元素与剩余切片（用于循环短路早退或动态工作队列） | `source` (`Array<T>`, 必填) | `item` (`T`, 必填，不可与 `rest` 同名)<br>`rest` (`Array<T>`, 必填，推荐与 `source` 同名原地覆盖) | 暴露 **`.some`**（定值 `item` 与 `rest`）与 **`.empty`**（不写入） |
| **[`builtin:array_append`](./array-append.md)** | 将单个元素追加到列表末尾 | `source` (`Array<T>`, 必填)<br>`item` (`T`, 必填) | `result` (`Array<T>`, 必填，推荐与 `source` 同名原地更新) | 无分支单出边（定值 `result`） |
| **[`builtin:map_set`](./map-set.md)** | 向有序映射插入或原地更新键值对（保持插入顺序） | `source` (`OrderedMap<string, TV>`, 必填)<br>`key` (`string`, 必填)<br>`value` (`TV`, 必填) | `result` (`OrderedMap<string, TV>`, 必填，推荐与 `source` 同名原地更新) | 无分支单出边（定值 `result`） |
