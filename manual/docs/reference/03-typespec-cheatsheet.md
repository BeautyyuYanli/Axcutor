# 03. TypeSpec 类型写法速查表

Axcutor 中所有的 `input`、`output` 及 `graph.variables` 均采用 **标准 JSON Schema 的闭合强类型子集（`TypeSpec`）**。

> [!IMPORTANT]
> **闭合性约束**：禁止使用无 `properties` 也无 `additionalProperties` 的裸 `object`、无 `items` 的裸 `array`、单独的 `"null"` 或 `any`。对于 `object` 类型，`properties`（固定字段结构体 `TypedDict`）与 `additionalProperties`（动态键有序映射 `OrderedMap`）必须**二选一**，不可同时声明或同时省略。

---

## 一、 五类标准 `TypeSpec` 构造汇总

| 构造类别 | YAML 必填/可选字段 | 适用场景 |
| :--- | :--- | :--- |
| **1. 基础标量与枚举** | `type: string \| integer \| number \| boolean`<br>`enum: [...]` *(可选)* | 字符串、整数、浮点数、布尔值及有限字面量枚举（若 `string` `enum` 用于 `builtin:switch` 路由，每个枚举值均须为合法分支标识符 `^[a-zA-Z_][a-zA-Z0-9_]*$` 且不可为 `start`/`end`/`fail`）。 |
| **2. 强类型列表（Array）** | `type: array`<br>`items: <TypeSpec>` *(必填)* | 同质元素有序列表（支持 `for_each` 遍历、`builtin:array_shift` 首元素弹出、`builtin:array_append` 末尾追加与 `builtin:array_flatten` 二维列表展平）。 |
| **3. 强类型结构体（TypedDict）** | `type: object`<br>`properties: { <k>: <TypeSpec> }` *(必填非空)*<br>`required: [...]` *(可选，省略时默认全部必填)* | 固定字段记录结构，支持通过 `builtin:object_merge` 不可变构造或覆盖更新字段，且非空时支持 `var.field` 静态字段路径投影（可配合 `builtin:assign` 提取嵌套字段）：中间穿透路径上的字段必须列入 `required` 且非空；最末级叶子字段若未列入 `required`，投影时静态类型自动提升为显式可空类型 `[<BaseType>, "null"]`（运行期缺失键时求值为 `null`，可通过 `builtin:unwrap` 拆包）。 |
| **4. 强类型有序映射（Ordered Map）** | `type: object`<br>`additionalProperties: <TypeSpec>` *(必填，不可为布尔值)* | 动态 `string` 键、同质值的有序字典（运行时保持插入顺序，支持 `for_each` 遍历以及 `builtin:map_get` 动态键查找、`builtin:map_set` 写入与 `builtin:map_delete` 移除）。 |
| **5. 显式可空类型（`Optional<T>`）** | `type: [<BaseType>, "null"]`<br>*(配套字段与 `<BaseType>` 一致)* | 表示可能为 `null` 的外部输入或可选字段（可通过 `builtin:unwrap` 拆包收窄为非空 `T`）。 |

---

## 二、 常用 YAML 模板速查

```yaml
# 1. 基础标量
count:
  type: integer
  description: "计数"

# 2. 强类型枚举（enum）
# 注：当用于 builtin:switch 时，枚举元素必须为合法标识符且不可为 start / end / fail
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
# - passed、score、issues 列入 required，静态投影（如 review_report.score）直接得到对应非空类型
# - reviewer_note 未列入 required（可选属性），作为叶子字段投影（review_report.reviewer_note）时
#   静态类型自动提升为 [string, "null"]，运行期缺失该键时求值为 null，可传入 builtin:unwrap 拆包
review_report:
  type: object
  properties:
    passed: { type: boolean }
    score: { type: number }
    issues:
      type: array
      items: { type: string }
    reviewer_note: { type: string }
  required: [passed, score, issues]

# 6. 强类型有序映射（Ordered Mapping：type: object + additionalProperties）
module_scores:
  type: object
  additionalProperties:
    type: number

# 7. graph.variables 中声明带 default 初值与未初始化局部变量
graph:
  variables:
    # 标量带初值声明（TypeSpec + default）
    retry_count:
      type: integer
      default: 0
    # 空数组初值声明（TypeSpec + default: []）
    collected_logs:
      type: array
      items: { type: string }
      default: []
    # 空有序映射初值声明（TypeSpec + default: {}）
    result_cache:
      type: object
      additionalProperties: { type: string }
      default: {}
    # 未初始化局部变量声明（仅声明 TypeSpec，省略 default，由后续节点写入定值）
    current_label:
      type: string
```
