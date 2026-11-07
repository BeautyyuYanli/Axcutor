# 配方 01：生成-校验-修正闭环（CFG 回边 + 分支关联输出）

## 一、 场景与需求

在“生成 SQL 或业务规则代码 → 在沙箱中执行验证 → 若报错则结合错误信息修正并重试（最多 3 次）”的闭环流程中，需满足两项核心需求：

1. **跨轮次演进状态**：每一轮生成节点均需读取上一轮沙箱返回的错误信息 `last_error` 与当前累计尝试次数 `attempts`。
2. **不同出口按需产出不同字段**：验证通过（`success`）时仅返回校验通过的 `sql` 与 `attempts`；超过最大重试次数失败（`failed`）时仅返回 `attempts` 与最后一次报错 `last_error`（此时不存在合法的 `sql`）。

---

## 二、 核心原语组合要点

1. **跨轮状态初值声明与确定性计数**：在 `graph.variables` 中声明 `attempts: { type: integer, default: 0 }` 与 `last_error: { type: string, default: "" }`，为首轮进入循环提供初始定值；在循环体入口通过 [`builtin:math`](../../reference/builtins/math.md)（`increment_attempts`）确定性地将 `attempts` 自增 `1`，并通过 [`builtin:compare`](../../reference/builtins/compare.md)（`check_retry_budget`）判断 `attempts < 3`，避免依赖业务代码手动维护循环计数与阈值判断。
2. **成环回边与退环出口**：在 `graph.edges` 中通过 `check_retry_budget.true -> increment_attempts` 构成成环回边，通过 `validate_sql.valid -> end.success` 与 `check_retry_budget.false -> end.failed` 构成两条退环出口。
3. **分支关联输出子集（`branches.<choice>.output`）**：在根节点输出全集中将 `sql`、`attempts`、`last_error` 均声明为非空类型，并在顶层 `branches` 中令 `success` 分支仅绑定 `output: [sql, attempts]`、`failed` 分支仅绑定 `output: [attempts, last_error]`。到达对应出口时，仅校验该分支声明的输出子集已定值，从而无需将 `sql` 或 `last_error` 妥协为可空类型。

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

  # 4. 跨循环轮次演进的初始状态变量（首轮进入循环时读取）
  variables:
    attempts:
      type: integer
      default: 0
    last_error:
      type: string
      default: ""

  nodes:
    # 步骤 1（循环体入口）：使用 builtin:math 确定性地将 attempts 自增 1
    increment_attempts:
      use: builtin:math
      with:
        input:
          op: { value: "add" }
          left: attempts
          right: 1
        output:
          result: attempts

    # 步骤 2：读取需求、表结构及上轮状态，生成或修正 SQL
    generate_sql:
      input: [schema, question, last_error, attempts]
      output:
        sql:
          type: string
      run:
        executor: builtin
        content: |
          sql_query = f"SELECT * FROM ({input.schema}) WHERE q = {input.question!r} -- attempt {input.attempts}"
          return Output(sql=sql_query)

    # 步骤 3：在沙箱中验证 SQL
    # - valid 分支声明 output: []，无需覆写 last_error
    # - invalid 分支声明 output: [last_error]，记录本轮错误原因
    validate_sql:
      input: [sql]
      output:
        last_error:
          type: string
      branches:
        valid:
          description: "SQL 语法正确且执行无报错"
          output: []
        invalid:
          description: "SQL 执行报错，记录错误原因并检查重试余量"
          output: [last_error]
      run:
        executor: builtin
        content: |
          if input.sql.upper().startswith("SELECT"):
              return Branch.valid()
          return Branch.invalid(last_error="Query must start with SELECT")

    # 步骤 4：使用 builtin:compare 确定性地判断是否仍有重试余量（attempts < 3）
    check_retry_budget:
      use: builtin:compare
      with:
        input:
          op: { value: "lt" }
          left: attempts
          right: 3

  # 5. 控制流连边：包含成环回边与两个分支出口
  edges:
    - start -> increment_attempts
    - increment_attempts -> generate_sql
    - generate_sql -> validate_sql
    # 校验通过：直接退环走向 end.success
    - validate_sql.valid -> end.success
    # 校验失败：检查尝试次数是否未达上限（attempts < 3）
    - validate_sql.invalid -> check_retry_budget
    # 成环回边：未达上限，携带更新后的 last_error 跳回 increment_attempts 进入下一轮
    - check_retry_budget.true -> increment_attempts
    # 退环出口：已达 3 次上限，走向 end.failed
    - check_retry_budget.false -> end.failed
```
