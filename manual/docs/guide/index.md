# 渐进式教程（Guide）

本教程系统讲解 Axcutor 工作流 DSL 的核心概念与编排原语。每节聚焦单一正交特性，配合局部语法片段与带注释的完整工作流说明其静态约束与运行时行为。

---

## 核心原语正交体系

Axcutor 的所有复杂编排能力均由以下四个正交维度组合而成：

| 维度 | 核心原语 | 解决的核心问题 |
| :--- | :--- | :--- |
| **1. 基础构建块与模块化** | `run`、`use`、内联 `graph` | 定义原子业务执行契约、跨文件函数调用栈帧（`Function Frame`）复用，以及单文件内单入口单出口（SESE）块级作用域（`Block Frame`）封装。 |
| **2. 条件分支与动态循环** | `branches`、`end.<branch>`、成环回边、`builtin:*` | 表达排他控制流分支与按需输出子集（Tagged Union）、未知迭代次数的动态收敛循环（`while`），以及零开销确定性路由与队列/集合操作。 |
| **3. 结构化并发编排** | `for_each`（`filter` / `collect` / `state`）、`-> [a, b] ->` | 表达同构集合的元素级并发遍历与保序汇聚，以及异构节点的 Fork-Join 并发同步与写集互斥保护。 |
| **4. 作用域、护栏与容错** | `variables`、`max_steps`、`timeout_ms`、`retry`、`catch`、`-> fail` | 管理栈帧变量生命周期与全路径必达定值，限制循环步数与调用深度，并通过快照重试、异常分支物化与子图回滚实现事务级容错。 |

---

## 原语组合结构概览

以下片段展示了上述四个维度的原语如何在同一张控制流图中协同工作（具体语法细节请通过左侧目录按章节阅读）：

```yaml
input:
  raw_items:
    type: array
    items: { type: string }
output:
  processed_items:
    type: array
    items: { type: string }
  audit_report:
    type: string

graph:
  # 1. 执行护栏：限制当前图最大步数
  max_steps: 100

  nodes:
    # 2. 同构结构化并发（for_each）+ 节点级超时重试（timeout_ms / retry）+ 异常隔离（catch）
    batch_process:
      for_each:
        source: raw_items
        item: single_item
        concurrency: 4
        collect:
          clean_item: processed_items
      timeout_ms: 3000
      retry:
        max_attempts: 2
        on: [execution, timeout]
      catch:
        skipped: "*"
      input: [single_item]
      output:
        clean_item: { type: string }
      run: "处理单条数据 variables.single_item，结果写入 output.clean_item。"

    # 3. 跨文件调用子工作流（use + with）
    generate_audit:
      use: workflow:compliance/build_audit_report
      with:
        input:
          items: processed_items
          strict: true
        output:
          report: audit_report

  # 4. 控制流连边（edges）
  edges:
    - start -> batch_process
    - batch_process -> generate_audit
    - generate_audit -> end
```
