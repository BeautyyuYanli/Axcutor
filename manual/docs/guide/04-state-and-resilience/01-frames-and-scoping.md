# 01. 图栈帧与命名空间（Stack Frames & Scoping）

在编写包含子工作流调用（`use`）、内联子图（`graph`）以及集合遍历（`for_each`）的工作流时，理解 Axcutor 的**栈帧边界**与**变量初始化规则**可以帮助你避免任何变量命名冲突或未初始化读取错误。

---

## 一、 两类栈帧与三层作用域心智模型

Axcutor 运行时通过两类核心栈帧管理变量的生命周期：

1. **函数调用栈帧（`Function Frame` —— 根工作流与 `use` 调用）**：
   * 每次通过 `use` 调用外部工作流（或 `builtin:*`）时，都会创建一个全新的、与调用方完全隔离的命名空间。
   * 外部工作流**看不到**父图的任何变量，只能拿到通过 `with.input` 显式传入的值；退出时也只能通过 `with.output` 将声明的输出写回父图。
2. **块级作用域栈帧（`Block Frame` —— 内联 `graph` 节点）**：
   * 图内的内联 `graph` 节点就像主流编程语言中的 `{ ... }` 块级作用域，但它采用**显式捕获（Explicit Capture）**：只有列入内联 `graph` 节点 `input: [<var>, ...]` 的外层变量（以及 `for_each` 注入的 `item`/`key`/`index` 和 `state`），才会进入子图的 `variables` 表。
   * **私有变量允许同名**：因为未列入 `input: [...]` 的外层变量对子图完全不可见，你在内联子图内部可以放心地使用 `retry_count`、`temp_draft` 等通用变量名，即使外层图也有一个未被捕获的 `retry_count`，两者也完全隔离、互不干扰！

---

## 二、 `graph.variables` 初值声明的两种写法

在 `graph.variables` 中声明图内部初始变量时，支持两种写法：

### 1. 标量字面量简写（`<var_name>: <literal>`）
当初始值是非空基础标量（字符串、整数、浮点数、布尔值），或者该变量已经在图内某个节点的 `output` 中声明了具体的 `TypeSpec` 时，可以直接写字面量初值：

```yaml
graph:
  variables:
    retry_count: 0              # 自动推导为 integer（或与下游 output 中的 integer/number 匹配）
    review_feedback: "首次生成"  # 自动推导为 string
```

### 2. 显式带类型声明（`<var_name>: { <TypeSpec>, default: <literal> }`）
当初始值是**空数组 `[]`**、**空字典 `{}`**、或者需要在无 `output` 节点写入的情况下声明带 `enum` / 可空类型的变量时，使用完整 `TypeSpec` + `default`：

```yaml
graph:
  variables:
    history_logs:
      type: array
      items: { type: string }
      default: []
    task_scores:
      type: object
      additionalProperties: { type: number }
      default: {}
```

---

## 三、 宿主输入与 `graph.variables` 初值互斥原则

为了防止“外部传入的值被 `graph.variables` 的默认初值意外覆盖”，Axcutor 有一条非常清晰的黄金法则：

> **凡是进入当前图时已经由外部提供的变量（即根图的顶层 `input`，或内联 `graph` 节点的 `input: [...]` 捕获变量、`for_each` 注入的 `item`/`key`/`index` 与 `state` 变量），严禁在当前图的 `graph.variables` 中重复声明同名初值！**

### 实战问答：当内联 `graph` 要对外 `output` 变量 `X` 时，怎么写？

* **情况 A（子图需要读取外层已有的 `X`，并在其基础上修改演进）**：
  将 `X` 写进内联 `graph` 节点的 `input: [..., X]` 中，**不要**在子图的 `graph.variables` 中声明 `X`。子图启动时会自动拿到外层 `X` 的当前值。
* **情况 B（`X` 是由子图首次创建的变量，或者子图完全重写 `X` 而不关心外层旧值）**：
  **不要**把 `X` 写进内联 `graph` 节点的 `input` 列表中。此时你可以在子图的 `graph.variables` 中为 `X` 设初值，或者在子图内部通往 `end` 的节点中产出 `X`。

---

## 四、 编译期全路径定值保护（永不出现 `undefined`）

在工作流运行之前，Axcutor 会自动检查图中的每一条控制流分支路径：
* 如果某个节点在 `input`（或 `with.input`）中声明要读取变量 `X`，那么**从 `start` 到达该节点的每一条可能路径上，`X` 都必须已经被赋值**。
* 如果某条分支（比如某个未赋值的旁路或异常恢复分支 `catch`）遗漏了对 `X` 的赋值就汇合到了读取 `X` 的节点，静态检查器会在运行前直接报错提醒你补齐赋值。

---

下一节我们将介绍如何为工作流配置步数护栏、调用深度保护以及节点级超时与原地重试：[02. 执行护栏：`max_steps`、调用栈深度与 `timeout_ms`](./02-guardrails-and-timeouts.md)。
