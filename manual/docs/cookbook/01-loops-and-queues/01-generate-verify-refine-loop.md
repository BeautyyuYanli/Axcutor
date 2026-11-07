# 配方 01：生成-校验-修正闭环（CFG 回边 + 分支关联输出）

## 一、 场景与需求

在“生成 SQL 或业务规则代码 → 在沙箱中执行验证 → 若报错则结合错误信息修正并重试（最多 3 次）”的闭环流程中，需满足两项核心需求：

1. **跨轮次演进状态**：每一轮生成节点均需读取上一轮沙箱返回的错误信息 `last_error` 与当前累计尝试次数 `attempts`。
2. **不同出口按需产出不同字段**：验证通过（`success`）时仅返回校验通过的 `sql` 与 `attempts`；超过最大重试次数失败（`failed`）时仅返回 `attempts` 与最后一次报错 `last_error`（此时不存在合法的 `sql`）。

---

## 二、 核心原语组合要点

1. **跨轮状态初值声明**：在 `graph.variables` 中声明 `attempts: { type: integer, default: 0 }` 与 `last_error: { type: string, default: "" }`，为首轮进入循环入口 `generate_sql` 提供初始定值。
2. **成环回边与退环出口**：在 `graph.edges` 中通过 `validate_sql.retry -> generate_sql` 构成回边，通过 `validate_sql.valid -> end.success` 与 `validate_sql.exhausted -> end.failed` 构成两条退环出口。
3. **分支关联输出子集（`branches.<choice>.output`）**：顶层 `success` 分支仅关联 `[sql, attempts]`，`failed` 分支仅关联 `[attempts, last_error]`，使 `sql` 保持非空 `string` 类型：

```yaml
output:
  sql: { type: string }
  attempts: { type: integer }
  last_error: { type: string }
branches:
  success:
    description: "SQL 生成并校验通过"
    output: [sql, attempts]          # 到达 end.success 时仅校验 sql 与 attempts 已定值
  failed:
    description: "达到最大重试次数仍存在错误"
    output: [attempts, last_error]   # 到达 end.failed 时仅校验 attempts 与 last_error 已定值
```

---

## 三、 完整工作流定义

```yaml
# 1. 根节点输入契约
input:
  question:
    type: string
    description: "用户的查询需求"
  schema:
    type: string
    description: "数据库表结构说明"

# 2. 根节点输出全集：各字段均为非空类型
output:
  sql:
    type: string
    description: "校验通过的 SQL 语句（仅在 success 分支产出）"
  attempts:
    type: integer
    description: "累计生成尝试次数"
  last_error:
    type: string
    description: "最后一次沙箱报错信息（仅在 failed 分支产出）"

# 3. 根节点排他分支契约：按出口分支绑定不同的输出字段子集
branches:
  success:
    description: "SQL 生成并校验通过"
    output: [sql, attempts]
  failed:
    description: "达到最大重试次数仍存在语法或执行错误"
    output: [attempts, last_error]

graph:
  # 限制单图最大调度步数，防止环路不收敛
  max_steps: 20

  # 4. 跨循环轮次演进的初始状态变量（首轮进入 generate_sql 时读取）
  variables:
    attempts:
      type: integer
      default: 0
    last_error:
      type: string
      default: ""

  nodes:
    # 步骤 1（循环体入口）：读取需求、表结构及上轮状态，生成或修正 SQL 并递增 attempts
    generate_sql:
      input: [schema, question, last_error, attempts]
      output:
        sql:
          type: string
        attempts:
          type: integer
      run:
        - "根据表结构 variables.schema 和查询需求 variables.question 编写 SQL。"
        - "若 variables.last_error 不为空，结合该错误信息修正上一轮 SQL。"
        - "将结果写入 output.sql，并将 variables.attempts + 1 写入 output.attempts。"

    # 步骤 2（循环条件判断）：在沙箱中验证 SQL
    # - valid 分支声明 output: []，无需覆写 last_error
    # - retry 与 exhausted 分支声明 output: [last_error]，记录本轮错误原因
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
          description: "执行报错且未超过 3 次尝试上限，回边重写"
          output: [last_error]
        exhausted:
          description: "执行报错且 variables.attempts 已达到 3 次"
          output: [last_error]
      run:
        - "在数据库沙箱中试运行 variables.sql。"
        - "若成功，选择 valid 分支。"
        - "若失败，将具体报错信息写入 output.last_error；若 variables.attempts < 3 则选择 retry 分支，否则选择 exhausted 分支。"

  # 5. 控制流连边：包含成环回边与两个分支出口
  edges:
    - start -> generate_sql
    - generate_sql -> validate_sql
    # 成环回边：携带更新后的 last_error 与 attempts 跳回 generate_sql
    - validate_sql.retry -> generate_sql
    # 退环出口：分别走向 end.success 与 end.failed
    - validate_sql.valid -> end.success
    - validate_sql.exhausted -> end.failed
```
