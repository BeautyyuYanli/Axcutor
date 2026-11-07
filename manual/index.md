# Axcutor 文档中心（Manual）

Axcutor 是一套基于**单令牌结构化并发控制流图（Structured CFG）**与**闭合静态强类型系统（JSON Schema 子集 `TypeSpec`）**的工作流编排语言与运行时引擎。

在 Axcutor 中，每个工作流文件本身即为一个对外暴露明确函数签名（$\text{Input} \to (\text{Branch}, \text{Output})$）的节点。图内的 `run` 节点代表任意满足该签名约束的业务代码（包括本地/远程函数、Python/JS 脚本沙箱、微服务 HTTP/RPC 接口、数据库事务或结构化模型调用），而类型安全、栈帧作用域隔离、并发无竞争同步与异常回滚均由编译器与运行时统一保障。

---

## 文档模块划分

全站文档按受众与工程职责划分为以下模块（可通过顶部导航栏与左侧目录切换）：

| 模块 | 目标受众 | 核心内容与职责边界 |
| :--- | :--- | :--- |
| **[渐进式教程（Guide）](/docs/guide/)** | 工作流编写者、业务开发者 | 按概念递进顺序讲解节点模型、`use` 调用、内联 `graph`、条件分支、成环回边、`for_each`、Fork-Join 并发组、栈帧作用域、执行护栏与 `catch`/`fail` 异常处理。 |
| **[实战组合配方（Cookbook）](/docs/cookbook/)** | 工作流编写者、架构师 | 针对典型业务场景（动态收敛闭环、候选短路回退链、有状态折叠、并发分流与展平、嵌套并发循环、多步原子事务、批处理死信隔离），提供多原语正交组合的端到端模板。 |
| **[用户速查字典（Reference）](/docs/reference/)** | 日常开发与自动化 Agent | 提供各层级 YAML 字段表、6 种控制边语法表、`TypeSpec` 类型构造模板以及 16 个零开销运行时内建算子（`builtin:*`）的参数字典。 |
| **[形式化规范（Spec）](/spec/)** | 编译器、运行时与执行器实现者 | 定义文法与 AST 构造、单遍显式符号表构建与模块化类型检查、CFG 拓扑与全路径必达定值分析、栈帧内存与并发调度状态机，以及 `run` 执行器标准 ABI 契约。 |
| **[早期原型归档（Prototype）](/prototype/)** | 语言设计研究者 | 归档早期探索阶段的原始设计文稿，仅供演进脉络参考。 |

---

## 最小工作流结构预览

```yaml
# 1. 顶层函数契约：输入与输出均采用闭合 JSON Schema 子集（TypeSpec）
input:
  order_id: { type: string }
output:
  receipt_id: { type: string }

# 2. 控制流图容器：由局部变量声明（variables）、节点定义（nodes）与控制边（edges）构成
graph:
  # 显式声明仅由外部调用节点写入的图内局部中间变量类型
  variables:
    amount: { type: number }

  nodes:
    # 调用外部独立工作流（Function Frame）
    verify_order:
      use: workflow:billing/verify_order
      with:
        input:
          id: order_id
        output:
          verified_amount: amount

    # 执行受签名约束的业务动作（Action Node）
    issue_receipt:
      input: [order_id, amount]
      output:
        receipt_id: { type: string }
      run: "为订单 variables.order_id（金额 variables.amount）签发收据，写入 output.receipt_id。"

  edges:
    - start -> verify_order
    - verify_order -> issue_receipt
    - issue_receipt -> end
```
