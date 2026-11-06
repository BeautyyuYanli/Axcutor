# 03. TypeSpec 类型写法速查表

Axcutor 中所有的 `input`、`output` 及显式带类型的 `graph.variables` 均使用 **标准 JSON Schema 的严格强类型子集（`TypeSpec`）**。

> **核心禁令**：严禁使用无 `properties` 也无 `additionalProperties` 的裸 `object`、无 `items` 的裸 `array`、单独的 `"null"` 或 `any`。对于 `object` 类型，`properties`（固定字段结构体）与 `additionalProperties`（动态键有序字典）必须**二选一**，不可同时出现或同时省略。

---

## 一、 五类标准 `TypeSpec` 构造汇总

| 构造类别 | YAML 必填/可选字段 | 适用场景 |
| :--- | :--- | :--- |
| **1. 基础标量与枚举** | `type: string \| integer \| number \| boolean`<br>`enum: [...]` *(可选)* | 字符串、整数、浮点数、布尔值及有限字面量枚举。 |
| **2. 强类型列表（Array）** | `type: array`<br>`items: <TypeSpec>` *(必填)* | 同质元素有序列表（支持 `for_each` 遍历）。 |
| **3. 强类型结构体（TypedDict）** | `type: object`<br>`properties: { <k>: <TypeSpec> }` *(必填非空)*<br>`required: [...]` *(可选，省略时默认全部必填)* | 固定字段记录结构（非空时支持 `var.field` 静态字段路径投影）。 |
| **4. 强类型有序映射（Ordered Map）** | `type: object`<br>`additionalProperties: <TypeSpec>` *(必填，不可为 `true`)* | 动态 `string` 键、同质值的有序字典（运行时保持插入顺序，支持 `for_each` 遍历）。 |
| **5. 显式可空类型（`Optional<T>`）** | `type: [<BaseType>, "null"]`<br>*(配套字段与 `<BaseType>` 一致)* | 表示可能为 `null` 的外部输入或可选字段（可通过 `builtin:unwrap` 拆包收窄为非空 `T`）。 |

---

## 二、 常用 YAML 模板速查

```yaml
# 1. 基础标量
count:
  type: integer
  description: "计数"

# 2. 强类型枚举（enum）
status:
  type: string
  enum: [pending, running, completed, failed]

# 3. 显式可空标量与可空结构体（Optional<T>）
optional_note:
  type: [string, "null"]

optional_user:
  type: [object, "null"]
  properties:
    uid: { type: string }
    age: { type: integer }
  required: [uid, age]

# 4. 强类型列表（一维与二维数组）
tag_list:
  type: array
  items:
    type: string

embedding_matrix:
  type: array
  items:
    type: array
    items:
      type: number

# 5. 强类型结构体（TypedDict：type: object + properties）
review_report:
  type: object
  properties:
    passed: { type: boolean }
    score: { type: number }
    issues:
      type: array
      items: { type: string }
  required: [passed, score, issues]

# 6. 强类型有序映射（Ordered Mapping：type: object + additionalProperties）
module_scores:
  type: object
  additionalProperties:
    type: number

# 7. graph.variables 中显式声明空数组 / 空映射初值
graph:
  variables:
    # 空数组初值必须显式带 TypeSpec + default: []
    collected_logs:
      type: array
      items: { type: string }
      default: []
    # 空有序映射初值必须显式带 TypeSpec + default: {}
    result_cache:
      type: object
      additionalProperties: { type: string }
      default: {}
```
