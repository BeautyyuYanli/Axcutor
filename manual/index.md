# Axcutor 文档中心（Manual）

Axcutor 是面向复杂业务编排的工作流语言与运行时引擎。它采用单令牌结构化并发控制流图（Structured CFG）描述执行拓扑，并通过闭合静态强类型系统（JSON Schema 子集 `TypeSpec`）约束全链路的数据契约。

在 Axcutor 中，每个工作流包（`__axcutor__.yaml`）或入口工作流文件本身都是一个具备明确函数签名的节点，其输入、控制分支与输出遵循统一的映射关系：$\text{Input} \to (\text{Branch}, \text{Output})$。图内的 `run` 节点通过结构化的 `executor` 契约承载任意满足该签名约束的业务实现，既支持内置受信任的强类型内联 Python 执行器（`executor: builtin`）与包内独立模块级 Python 执行器（`executor: module`），也可扩展至脚本沙箱、HTTP/RPC 微服务接口、数据库事务以及结构化模型调用。类型检查、栈帧作用域隔离、并发无锁同步与异常原子回滚均由编译器和运行时统一保障。

---

## 核心设计原则

### 1. 统一节点模型

工作流包、跨包调用节点（`use`）、内联子图（`graph`）、集合迭代（`for_each`）与业务动作节点（`run`）共享同一节点抽象。无论内部是单步外部调用还是嵌套循环的子图，所有节点对外均暴露相同的 $\text{Input} \to (\text{Branch}, \text{Output})$ 契约，支持任意层级的正交组合与模块化复用。

### 2. 闭合静态强类型与全路径必达定值

所有输入、输出与图内局部变量均通过闭合的 `TypeSpec`（默认遵循 `additionalProperties: false` 语义）显式声明或推导。编译器在执行前静态完成符号表构建、跨节点类型兼容性检查与全路径必达定值分析（Definite Assignment Analysis），确保任何变量与输出字段在被读取或返回前，在所有可达控制流路径上均已完成定值，从编译期消除未初始化读取与隐式字段泄漏。

### 3. 结构化并发与无锁确定性

常规控制流采用单执行令牌（Single Token）沿有向边推进；并发执行严格限定在结构化的 Fork-Join 并发组与 `for_each` 并行迭代内。编译器静态校验并发分支的写入目标互不相交，或仅通过带确定性槽位（Slot）的 `append` 汇聚，无需用户手动加锁即可在运行时实现无数据竞争（Data-Race-Free）的确定性并发。

### 4. 栈帧级异常捕获与原子回滚

每次调用子工作流、进入内联子图或执行单次循环迭代，运行时都会开辟独立的栈帧（Stack Frame）与局部状态空间。当节点触发异常或显式执行 `fail` 时，若当前层级未声明匹配的 `catch` 策略，运行时将自动丢弃该栈帧内的全部中间修改，并向父栈帧冒泡异常，从而保证跨层级调用的原子回滚与故障隔离。

---

## 最小工作流结构预览

以下示例展示了一个完整工作流文件的核心构成：顶层输入输出契约、图内局部变量声明、节点定义以及控制流边。

```yaml
# 1. 顶层函数契约：输入与输出均采用闭合 JSON Schema 子集（TypeSpec）
input:
  order_id: { type: string }
output:
  receipt_id: { type: string }

# 2. 控制流图容器：由局部变量声明（variables）、节点定义（nodes）与控制边（edges）构成
graph:
  # 显式声明仅由外部调用节点写入的图内局部变量类型
  variables:
    amount: { type: number }

  nodes:
    # 调用外部独立工作流包（创建独立函数栈帧）
    verify_order:
      use: workflow:billing.verify_order
      with:
        input:
          id: order_id
        output:
          verified_amount: amount

    # 执行受签名约束的业务动作节点
    issue_receipt:
      input: [order_id, amount]
      output:
        receipt_id: { type: string }
      run:
        executor: builtin
        content: |
          return Output(receipt_id=f"RCPT-{input.order_id}-{input.amount:.2f}")

  edges:
    - start -> verify_order
    - verify_order -> issue_receipt
    - issue_receipt -> end
```
