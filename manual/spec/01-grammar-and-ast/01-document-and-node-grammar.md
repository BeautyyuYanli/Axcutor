# 01. 文档根结构与三类节点文法规范

本规范定义 Axcutor 工作流文档的顶层 AST 结构、图内三类节点（`run`、`graph`、`use`）的形态鉴别准则以及分支关联输出契约（`BranchSpec`）的良构条件。

---

## 一、 顶层根节点（`RootNode`）文法

每个工作流文件反序列化后必须是一个映射对象（`RootNode`），满足以下文法约束：

1. **必填契约字段**：
   * `input: Map<Identifier, TypeSpec>`（可为空映射 `{}`）
   * `output: Map<Identifier, TypeSpec>`（可为空映射 `{}`）
2. **可选契约字段**：
   * `branches: Map<BranchId, BranchSpec>`（默认 `{}`）
3. **执行体互斥鉴别（Mutual Exclusion）**：
   * `RootNode` **必须且只能在 `run` 与 `graph` 两个键中声明其一**：
     * **当包含 `run`（`RootRunNode`）**：允许额外声明 `timeout_ms?: PositiveInt` 与 `retry?: RetrySpec`；禁止声明 `for_each`、`catch`、`with`、`variables`、`nodes`、`edges`、`max_steps`。
     * **当包含 `graph`（`RootGraphNode`）**：禁止在顶层声明 `timeout_ms`、`retry`、`for_each`、`catch`、`with`。

---

## 二、 图内节点（`InnerNode`）的三态鉴别与字段约束

在 `graph.nodes: Map<NodeId, InnerNode>` 中，每个键 `<NodeId>` 必须为合法标识符，且**禁止使用保留字 `start`、`end`、`fail`**。

每个 `InnerNode` 必须通过鉴别键 **`run`、`graph`、`use` 三选一** 唯一确定其 AST 子类型：

| AST 节点类型 | 唯一鉴别键 | 允许的可选字段 | 禁止出现的字段 |
| :--- | :--- | :--- | :--- |
| **`ActionNode`** | `run` | `input: List<Identifier>`<br>`output: Map<Identifier, TypeSpec>`<br>`branches: Map<BranchId, BranchSpec>`<br>`for_each: ForEachSpec`<br>`timeout_ms: PositiveInt`<br>`retry: RetrySpec`<br>`catch: CatchSpec` | `with`, `graph`, `use` |
| **`InlineGraphNode`** | `graph` | `input: List<Identifier>`<br>`output: List<Identifier> \| Map<Identifier, TypeSpec>`<br>`branches: Map<BranchId, BranchSpec>`<br>`for_each: ForEachSpec`<br>`timeout_ms: PositiveInt`<br>`retry: RetrySpec`<br>`catch: CatchSpec` | `with`, `run`, `use` |
| **`CallNode`** | `use` | `with: WithSpec`<br>*(以下仅当 `use` 为 `workflow:<path>` 时允许，为 `builtin:<op>` 时禁止)*：<br>`for_each: ForEachSpec`<br>`timeout_ms: PositiveInt`<br>`retry: RetrySpec`<br>`catch: CatchSpec` | `input`, `output`, `branches`, `run`, `graph` |

* **`input` 列表去重约束**：在 `ActionNode` 与 `InlineGraphNode` 中，`input` 若声明，必须为无重复元素的顶层变量标识符列表 `List<Identifier>`。
* **`InlineGraphNode.output` 列表简写去重约束**：当 `InlineGraphNode.output` 为 `List<Identifier>` 时，列表元素必须为无重复的标识符（等价于从子图内部符号表推导对应 `TypeSpec`）。

---

## 三、 分支关联输出契约（`BranchSpec`）规范化

在 `RootNode`、`ActionNode` 或 `InlineGraphNode` 的 `branches: Map<BranchId, BranchSpec>` 中：

* 每个 `<BranchId>` 必须为合法标识符，不可为保留字 `start`、`end`、`fail`，且不可与同节点 `catch` 中的异常分支名重复。
* 每个 `BranchSpec` 在 AST 规范化阶段统一消糖为二元组 `(description: String, output_subset: Set<Identifier>)`：
  1. **简写形式**（`<choice>: "<description>"`）：消糖为 `output_subset = dom(node.output)`（即承诺产出该节点 `output` 声明的全部字段）。
  2. **显式子集形式**（`<choice>: { description: "<description>", output: [<field_1>, ...] }`）：
     * `output` 必须为无重复标识符列表；
     * **子集良构性条件**：$\text{set}(\text{branch.output}) \subseteq \text{dom}(\text{node.output})$。若引用了未在当前节点 `output` 中声明的字段名，编译期报错。
