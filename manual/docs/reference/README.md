# 用户速查字典（User Reference）

本目录是面向工作流编写者的语法与参数速查字典，适合在日常编写 YAML 工作流时随手翻阅字段名、连边语法、类型声明写法以及内建算子参数。

*(注：若需查阅编译器符号表决议算法、全路径静态定值方程或运行时栈帧状态机等引擎实现规范，请前往顶层的 **[`spec/`](../../spec/README.md)** 目录。)*

---

## 字典目录

1. **[01. 节点与图字段速查表](./01-node-and-graph-fields.md)**
   根节点、图内 `run` / `use` / 内联 `graph` 节点、`graph` 容器、`for_each`、`retry` 与 `catch` 的完整 YAML 字段参考表。
2. **[02. 6 种控制边语法速查表](./02-edge-syntax-cheatsheet.md)**
   `graph.edges` 支持的 6 种标准连边格式与使用规则速查。
3. **[03. TypeSpec 类型写法速查表](./03-typespec-cheatsheet.md)**
   基础标量与 `enum`、强类型列表、强类型结构体（`TypedDict`）、强类型有序映射（`Ordered Mapping`）以及显式可空类型 `Optional<T>` 的 YAML 模板。
4. **[运行时内建节点字典（`builtins/`）](./builtins/README.md)**
   * [`builtin:unwrap`](./builtins/unwrap.md)：显式可空类型拆包（`.some`/`.none`）与默认值合并（`default`）
   * [`builtin:switch`](./builtins/switch.md)：`boolean`（`.true`/`.false`）与 `enum`（`.<enum_val>`）确定性路由
   * [`builtin:array_shift`](./builtins/array-shift.md)：列表首元素弹出与剩余切片（`.some`/`.empty`）
   * [`builtin:array_append`](./builtins/array-append.md)：列表末尾追加元素
   * [`builtin:map_set`](./builtins/map-set.md)：有序映射键值插入或更新
