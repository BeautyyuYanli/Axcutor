# 早期原型设计稿归档（Prototype Archive）

本目录归档了 Axcutor 工作流 DSL 在早期探索阶段的原始设计文稿，仅供追溯语言设计演进脉络参考。

> [!NOTE]
> **规范优先级说明**：早期原型文稿中的部分语法细节在正式版规范中已做收敛与统一。编写工作流或实现引擎时，请以 **[文档中心首页](../index.md)**、**[用户文档（Guide）](../docs/guide/index.md)** 与 **[形式化规范（Spec）](../spec/index.md)** 为准。

---

## 从早期原型到正式规范的关键演进

对比早期探索阶段的设计讨论，当前正式版 DSL 完成的主要收敛如下：

| 设计维度 | 早期探索阶段方案 | 当前正式版方案（`docs/guide/` & `spec/`） |
| :--- | :--- | :--- |
| **文档顶层结构** | 曾讨论使用外层包装字段 | 采用**统一节点模型（Unified Node Model）**：文件本身即为 `RootNode`，直接在顶层声明 `input`、`output`、`branches` 与 `run \| graph`。 |
| **内联子图输出与符号解析** | 曾探讨 `output: [<var>, ...]` 列表简写与两阶段符号推导 | 内联 `graph` 节点与 `run` 节点一样显式声明 **`output: { <var>: <TypeSpec> }`**；编译器采用**单遍显式符号表构建（Single-Pass Explicit Symbol Resolution）**与**单向静态子类型兼容（`<:`）**。 |
| **`for_each` 数据聚合与状态迭代** | 曾设置专用的 `filter`、`collect`、`state` 子块 | 移除专用子块，直接复用节点自身的 **`output`、`with.output` 与 `branches.<branch>.output`** 完成数组收集与分支过滤；有状态折叠改由 **`builtin:array_shift` + 成环回边**统一表达。 |
| **常量字面量传参** | 字符串与变量名引用边界需额外区分 | 在 `with.input` 中非字符串标量直接书写，字符串与复合常量统一通过 **`{ value: <literal> }`** 包装消歧。 |
| **循环与并发容器** | 分别探讨循环与遍历的组合边界 | 统一以 **SESE 内联 `graph`（`Block Frame`）** 作为唯一块级组合容器，支持在 `for_each` 或 Fork-Join 分支内任意嵌套成环回边与 `catch` 事务。 |
