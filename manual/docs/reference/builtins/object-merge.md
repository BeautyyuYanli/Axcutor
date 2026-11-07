# `builtin:object_merge`：结构体不可变构造与字段更新

在保持图级变量“顶层原子写回（禁止 `user.age = 20` 原地可变副作用）”的前提下，支持以纯函数式不可变方式**构造新的强类型结构体（`TypedDict`）**，或**基于已有结构体 `base` 不可变地覆盖更新部分字段**（等价于 TypeScript `{ ...base, field: new_val }` 或 Rust 结构体更新语法 `User { status: new_status, ..base }`）。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `base` | 可选 | 作为不可变更新基准的已有结构体引用。若声明，必须绑定已定值的非空强类型结构体（`TypedDict`）。 |
| **`with.input`** | `<field_1>` ... `<field_m>` | **至少 1 个** | 除保留键 `base` 外的其余每个键代表目标结构体（由 `with.output.result` 指定）的顶层属性名，支持绑定变量、静态字段路径或常量字面量。每个字段名必须已在目标结构体的 `properties` 中定义，且传入值的类型必须与目标对应属性的类型兼容（如 `integer` 兼容 `number`、非空类型天然兼容可空目标属性）。 |
| **`with.output`** | `result` | **必填** | 写入目标顶层结构体变量。该变量必须已在当前图中显式声明为非空强类型结构体（`TypedDict`：`type: object` 且包含非空 `properties`）。当声明了 `base` 时，允许 `result` 与 `base` 绑定同一个顶层变量名以完成不可变更新。 |

### 两种使用模式的静态校验规则

* **模式 A：从字段全新构造结构体（省略 `base`）**
  * **字段范围**：`with.input` 中提供的所有字段名必须属于目标结构体 `properties` 中已声明的属性，禁止传入未声明的额外字段。
  * **必填字段覆盖**：传入的字段集合必须完整覆盖目标结构体 `required` 中声明的全部必填属性。
  * **字段类型兼容**：每个字段绑定的引用表达式类型或常量字面量，必须与目标结构体对应属性的类型子类型兼容。
* **模式 B：基于已有结构体不可变覆盖更新（传入 `base`）**
  * **属性集范围**：`base` 结构体声明的全部属性名必须包含在目标结构体的 `properties` 属性集中，且本次显式传入的覆盖字段名也必须属于目标结构体的 `properties`。
  * **必填字段覆盖**：`base` 自身已保证的必填属性（`base.required`）与本次显式传入的覆盖字段合并后，必须完整覆盖目标结构体 `required` 中声明的全部必填属性。
  * **字段类型兼容**：本次显式传入的覆盖字段必须与目标结构体对应属性类型兼容；同时，`base` 中**未被本次显式覆盖**的其余保留属性，其原有类型也必须与目标结构体对应属性类型子类型兼容。

### 控制流与纯函数式不可变行为

* **无分支单出边（`node -> target`）**：以纯函数式不可变方式（Copy-on-Write）构造新结构体对象（若提供了 `base` 则先浅拷贝 `base` 的全部属性，再用 `with.input` 中列出的字段值覆盖对应属性）并原子绑定至 `result`，绝不原地修改输入快照引用中的旧结构体对象。

---

## 二、 YAML 用法示例

```yaml
graph:
  # 若目标结构体变量未在宿主 output 或其他 run/graph 节点的 output 中声明，须在 variables 中显式声明 TypedDict 类型
  variables:
    summary_record:
      type: object
      properties:
        doc_id: { type: string }
        score: { type: number }
        passed: { type: boolean }
      required: [doc_id, score, passed]

  nodes:
    # 模式 B（传入 base）：基于已有结构体 user_profile 不可变更新 status 与 updated_at 字段，并写回 user_profile
    update_user_status:
      use: builtin:object_merge
      with:
        input:
          base: user_profile                 # 基础 TypedDict 变量
          status: { value: "verified" }      # 用常量字面量覆盖字段 status
          updated_at: current_timestamp      # 用顶层变量覆盖字段 updated_at
        output:
          result: user_profile               # 原子写回顶层变量 user_profile

    # 模式 A（省略 base）：按照 summary_record 已声明的 TypedDict 契约，从多个变量、路径投影与标量常量组装出新结构体实例
    build_summary_record:
      use: builtin:object_merge
      with:
        input:
          doc_id: current_doc.id
          score: final_score                 # final_score 为 integer 或 number 均可子类型兼容写入 number 属性
          passed: true
        output:
          result: summary_record

  edges:
    - start -> update_user_status
    - update_user_status -> build_summary_record
    - build_summary_record -> end
```
