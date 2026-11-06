# 早期原型设计稿归档（Prototype Archive）

本目录保留 Axcutor 工作流 DSL 在早期探索阶段的 5 份原始设计文稿，供设计演进参考：

* **[DSL.md](./DSL.md)**：初始工作流 DSL 总体架构、强类型系统、`for_each` 与结构化并发控制流图规范
* **[builtins.md](./builtins.md)**：运行时内建算子（`builtin:*`）初稿
* **[error_handling.md](./error_handling.md)**：工作流异常处理与容错模型初稿
* **[while_and_iter.md](./while_and_iter.md)**：循环（`while`）、内建算子与 `for_each` 基础模式示例初稿
* **[while_plus_iter.md](./while_plus_iter.md)**：嵌套遍历与条件循环组合模式（`for_each` + SESE 内联 `graph`）初稿
