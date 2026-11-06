# 配方 01：生成-校验-修正闭环（CFG 回边 + 分支关联输出）

## 场景与问题

在编写“生成 SQL / 业务规则代码 → 在沙箱中验证 → 若报错则根据错误信息修正并重试（最多 3 次）”的闭环工作流时，面临两个常见诉求：
1. **跨轮次演进状态**：每一轮生成需要读取上一轮沙箱的报错信息 `last_error` 与当前尝试次数 `attempts`。
2. **不同出口产出不同字段**：验证通过（`success`）时只需返回合法的 `sql` 和尝试次数 `attempts`；而超过重试上限失败（`failed`）时只需返回 `attempts` 和最后一次报错 `last_error`（此时并没有合法的 `sql`）。

## 解决方案要点

* 使用 `graph.edges` 中的**成环回边** `validate_sql.retry -> generate_sql` 表达动态循环；
* 在 `graph.variables` 中为 `attempts: 0` 和 `last_error: ""` 提供首轮进入时的初始定值；
* 利用**分支关联输出契约（`branches.<choice>.output`）**，让 `success` 分支仅关联 `[sql, attempts]`，让 `failed` 分支仅关联 `[attempts, last_error]`，从而保持 `sql` 为纯正的非空 `string`。

---

## 完整工作流定义

```yaml
input:
  question:
    type: string
    description: "用户的查询需求"
  schema:
    type: string
    description: "数据库表结构说明"
output:
  sql:
    type: string
    description: "校验通过的 SQL 语句（仅在 success 分支产出）"
  attempts:
    type: integer
    description: "总共尝试生成的次数"
  last_error:
    type: string
    description: "最后一次沙箱报错信息（仅在 failed 分支产出）"
branches:
  success:
    description: "SQL 生成并校验通过"
    output: [sql, attempts]
  failed:
    description: "达到最大重试次数仍存在语法或执行错误"
    output: [attempts, last_error]

graph:
  max_steps: 20
  # 声明跨循环轮次演进的初始状态
  variables:
    attempts: 0
    last_error: ""

  nodes:
    # 1. 循环体起点：生成或修正 SQL
    generate_sql:
      input: [schema, question, last_error, attempts]
      output:
        sql:
          type: string
        attempts:
          type: integer
      run:
        - "根据表结构 variables.schema 和问题 variables.question 编写 SQL。"
        - "如果 variables.last_error 不为空，请结合该错误信息修正上一版 SQL。"
        - "将结果写入 output.sql，并将 variables.attempts 加一后写入 output.attempts。"

    # 2. 循环条件判断点（valid 分支声明 output: []，无需覆写 last_error）
    validate_sql:
      input: [sql, attempts]
      output:
        last_error:
          type: string
      branches:
        valid:
          description: "SQL 语法正确且执行无报错"
          output: []
        retry:
          description: "执行报错，但未超过 3 次尝试上限，打回重写"
          output: [last_error]
        exhausted:
          description: "执行报错，且 variables.attempts 已达到 3 次"
          output: [last_error]
      run:
        - "在数据库沙箱中试运行 variables.sql。"
        - "若成功，直接选择 valid 分支。"
        - "若失败，将具体报错信息写入 output.last_error；若 variables.attempts < 3 则选择 retry 分支，否则选择 exhausted 分支。"

  edges:
    - start -> generate_sql
    - generate_sql -> validate_sql
    # CFG 成环回边：携带更新后的 last_error 跳回 generate_sql
    - validate_sql.retry -> generate_sql
    # 循环出口（各出口分支仅校验其关联的 output 子集定值）
    - validate_sql.valid -> end.success
    - validate_sql.exhausted -> end.failed
```
