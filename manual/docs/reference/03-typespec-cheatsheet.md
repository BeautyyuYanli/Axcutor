# 03. TypeSpec 类型写法速查表

Axcutor 中所有的 `input`、`output` 及 `graph.variables` 均采用 **标准 JSON Schema 的闭合强类型子集（`TypeSpec`）**。

> [!IMPORTANT]
> **闭合性约束**：禁止使用无 `properties` 也无 `additionalProperties` 的裸 `object`、无 `items` 的裸 `array`、单独的 `"null"` 或 `any`。对于 `object` 类型，`properties`（固定字段结构体 `TypedDict`）与 `additionalProperties`（动态键有序映射 `OrderedMap`）必须**二选一**，不可同时声明或同时省略。

---

## 一、 `TypeSpec` 构造与可空修饰

| 构造类别 | YAML 必填/可选字段 | 适用场景 |
| :--- | :--- | :--- |
| **1. 基础标量与枚举** | `type: string \| integer \| number \| boolean`<br>`enum: [...]` *(可选)* | 字符串、整数、浮点数、布尔值及有限字面量枚举（若 `string` `enum` 用于 `builtin:switch` 路由，每个枚举值均须为合法受限标识符 `^[a-zA-Z_][a-zA-Z0-9_]*$` 且不可为 `start`/`end`/`fail` 或 Python 硬关键字）。 |
| **2. 强类型列表（Array）** | `type: array`<br>`items: <TypeSpec>` *(必填)* | 同质元素有序列表（支持 `for_each` 遍历、`builtin:length` 长度计算、`builtin:array_get` 下标随机访问、`builtin:array_shift` 首元素弹出、`builtin:array_append` 末尾追加、`builtin:array_concat` 列表拼接与 `builtin:array_flatten` 二维列表展平）。 |
| **3. 强类型结构体（TypedDict）** | `type: object`<br>`properties: { <k>: <TypeSpec> }` *(必填非空，键名 `<k>` 须为合法受限标识符)*<br>`required: [...]` *(可选，省略时默认全部必填)* | 固定字段记录结构（支持通过 `builtin:object_merge` 不可变构造或覆盖更新字段；非空时支持 `var.field` 静态字段路径投影并配合 `builtin:assign` 提取嵌套字段，详见下方投影规则）。 |
| **4. 强类型有序映射（Ordered Map）** | `type: object`<br>`additionalProperties: <TypeSpec>` *(必填，不可为布尔值)* | 动态 `string` 键、同质值的有序字典（运行时保持插入顺序，支持 `for_each` 遍历、`builtin:length` 键值对计数，以及 `builtin:map_get` 动态键查找、`builtin:map_set` 写入与 `builtin:map_delete` 移除）。 |
| **5. 资源句柄（Handle）** | `type: handle`<br>`kind: <ModulePath>:<PublicClassName>` | 内部受管资源身份；同 kind 兼容，不支持字段投影。详见[资源句柄](./05-resource-handles.md)。 |
| **显式可空修饰（`Optional<T>`）** | `type: [<BaseType>, "null"]`<br>*(配套字段与 `<BaseType>` 一致)* | 可通过 `builtin:unwrap` 收窄为非空 T；根接口及 signal 输出禁止句柄，包括可空句柄和句柄容器。 |

> [!NOTE]
> **结构体静态字段路径投影规则（`.field`）**
> * **适用前提**：仅非空 `TypedDict`（`type: object` 且声明 `properties`）支持通过 `var.field.subfield` 语法进行静态字段路径投影（常配合 `builtin:assign` 提取嵌套字段）。
> * **中间穿透路径约束**：多层投影路径上的所有**中间层字段**必须列入对应结构体的 `required` 且静态类型为非空 `TypedDict`。
> * **末级可选字段自动可空提升**：路径**最末级叶子字段**若已列入 `required`，投影结果为该字段原声明的 `<TypeSpec>`；若未列入 `required`（可选属性），投影时的静态类型自动提升为显式可空类型 `[<BaseType>, "null"]`（运行期缺失该键时求值为 `null`，可通过 `builtin:unwrap` 拆包）。

---

## 二、 常用 YAML 模板速查

```yaml
# 1. 基础标量
count:
  type: integer
  description: "计数"

# 2. 强类型枚举（enum）
# 注：当用于 builtin:switch 时，枚举元素必须为合法受限标识符（不可为 start / end / fail 或 Python 硬关键字）
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

---

## 三、 静态子类型赋值兼容性与不可变容器协变（$T_{\text{actual}} <: T_{\text{expected}}$）

Axcutor 严格区分**同帧同名变量声明（双向等价 $T_1 \equiv T_2$）**与**跨变量值流转及传参（单向子类型兼容 $T_{\text{actual}} <: T_{\text{expected}}$）**：

* **同帧同名显式声明（$T_1 \equiv T_2$）**：同一张图内若多处显式声明同一个变量名的 `TypeSpec`（如同时在宿主 `output` 与 `ActionNode.output` 中声明），两处声明必须完全等价。
* **值流转与契约消费（$T_{\text{actual}} <: T_{\text{expected}}$）**：在 `use: workflow:<PackagePath>` 传参与输出接收、`builtin:*` 算子赋值/更新以及 `for_each` 一阶集合提升写入位置，均支持以下五条静态子类型兼容规则（基于纯函数式写时复制不可变内存模型，容器协变在编译期 100% 类型安全）：

| 子类型规则 | 兼容方向（$T_{\text{sub}} <: T_{\text{sup}}$） | 典型应用场景 |
| :--- | :--- | :--- |
| **1. 可空性单调提升** | 非空 `T` $<:$ 显式可空 `[T, "null"]`<br>*(反向禁止：可空不可直接赋给非空，须先 `builtin:unwrap`)* | 将已确认非空的 `string` 变量传入子工作流的可选参数 `[string, "null"]`，或赋给初始为 `null` 的可空状态变量。 |
| **2. 数值与枚举拓宽** | • `integer` $<:$ `number`<br>• 子枚举 `enum: [a, b]` $<:$ 父枚举 `enum: [a, b, c]` $<:$ 无 `enum` 基础标量 | 将 `integer` 计数传给期望 `number` 的计算节点；将带 `enum` 的分类状态传给通用 `string` 日志参数。 |
| **3. 列表协变（`Array`）** | 若 $T_1 <: T_2$，则 `Array<T1>` $<:$ `Array<T2>` | 将 `Array<integer>` 赋给 `Array<number>`，或将 `Array<string(enum)>` 传给期望 `Array<string>` 的子工作流。 |
| **4. 有序映射协变（`OrderedMap`）** | 若 $V_1 <: V_2$，则 `OrderedMap<string, V1>` $<:$ `OrderedMap<string, V2>` | 将非空值映射 `OrderedMap<string, string>` 赋给允许空值的 `OrderedMap<string, [string, "null"]>`。 |
| **5. 结构体深度协变与必填强化（`TypedDict`）** | 若 $T_1 <: T_2$，须同时满足：<br>• **必填集强化**：$\text{set}(T_2.\text{required}) \subseteq \text{set}(T_1.\text{required})$（目标必填字段在源类型中均必填）<br>• **字段集闭合**：$\text{dom}(T_1.\text{properties}) \subseteq \text{dom}(T_2.\text{properties})$（源类型不含目标未声明的多余字段）<br>• **共有属性协变**：$\forall f \in \text{dom}(T_1.\text{properties}),\ T_1.\text{properties}[f] <: T_2.\text{properties}[f]$ | 将全部字段必填且非空的精确结构体，赋给部分字段可选（非 `required`）或字段类型为可空/父类型的同构目标结构体。 |

```yaml
graph:
  variables:
    # 目标变量声明为更宽泛的父类型
    nullable_winner:
      type: [string, "null"]
      default: null
    general_status:
      type: string
    metric_values:
      type: array
      items: { type: number }

  nodes:
    # 1. 非空提升：将非空 string 变量 verified_user 直接赋给 [string, "null"] 变量 nullable_winner
    promote_to_nullable:
      use: builtin:assign
      with:
        input:
          source: verified_user
        output:
          value: nullable_winner

    # 2. 枚举拓宽：将 { type: string, enum: [open, closed] } 的 ticket_status 赋给普通 string 变量
    widen_enum_to_string:
      use: builtin:assign
      with:
        input:
          source: ticket_status
        output:
          value: general_status

    # 3. 不可变列表协变：将 Array<integer> 类型的 raw_counts 赋给 Array<number> 类型的 metric_values
    covariant_array_assign:
      use: builtin:assign
      with:
        input:
          source: raw_counts
        output:
          value: metric_values
```
