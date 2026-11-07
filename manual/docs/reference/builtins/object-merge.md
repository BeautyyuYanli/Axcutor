# `builtin:object_merge`：结构体不可变构造与字段更新

在保持图级变量“顶层原子写回（禁止 `user.age = 20` 原地可变副作用）”的前提下，支持以纯函数式不可变方式**构造新的强类型结构体（`TypedDict`）**，或**基于已有结构体 `base` 不可变地覆盖更新部分字段**（等价于 TypeScript `{ ...base, field: new_val }` 或 Rust 结构体更新语法 `User { status: new_status, ..base }`）。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `base` | 可选 | 若声明，必须绑定已定值的非空强类型结构体（`TypedDict`），且其类型必须与目标输出变量 `result` 的声明类型 $T_{\text{target}} = \Sigma(v_{\text{res}})$ 静态等价。 |
| **`with.input`** | `<field_1>` ... `<field_m>` | **至少 1 个** | 除保留键 `base` 外的其余每个键代表目标结构体 $T_{\text{target}}$ 的顶层属性名，支持绑定变量、静态字段路径或常量字面量：<br>• **声明了 `base` 时**：每个 `<field_i>` 必须属于 $T_{\text{target}}.\text{properties}$ 已声明的属性集，且绑定值类型与对应属性类型兼容；<br>• **未声明 `base` 时**：提供的字段集合必须覆盖 $T_{\text{target}}$ 的全部必填属性且不超出声明属性集（$\text{set}(T_{\text{target}}.\text{required}) \subseteq \{f_1, \dots, f_m\} \subseteq \text{dom}(T_{\text{target}}.\text{properties})$），且各字段类型与对应属性类型兼容。 |
| **`with.output`** | `result` | **必填** | 写入目标顶层结构体变量 $v_{\text{res}}$。要求 $v_{\text{res}}$ 必须已在当前图符号表 $\Sigma$ 中显式声明为非空强类型结构体（`TypedDict`：`type: object` + 非空 `properties`）。当声明了 `base` 时，允许 `result` 与 `base` 绑定同一个顶层变量名以完成不可变更新。 |

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
    # 模式 A（传入 base）：基于已有结构体 user_profile 不可变更新 status 与 updated_at 字段，并写回 user_profile
    update_user_status:
      use: builtin:object_merge
      with:
        input:
          base: user_profile                 # 基础 TypedDict 变量
          status: { value: "verified" }      # 用常量字面量覆盖字段 status
          updated_at: current_timestamp      # 用顶层变量覆盖字段 updated_at
        output:
          result: user_profile               # 原子写回顶层变量 user_profile

    # 模式 B（省略 base）：按照 summary_record 已声明的 TypedDict 契约，从多个变量、路径投影与标量常量组装出新结构体实例
    build_summary_record:
      use: builtin:object_merge
      with:
        input:
          doc_id: current_doc.id
          score: final_score
          passed: true
        output:
          result: summary_record

  edges:
    - start -> update_user_status
    - update_user_status -> build_summary_record
    - build_summary_record -> end
```
