# `builtin:length`：集合与字符串长度测量

非破坏性地获取非空强类型列表 `Array<T>`、有序映射 `OrderedMap<string, V>` 或字符串 `string` 的元素/字符长度，并将非负整数结果写入目标变量。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空列表 `Array<T>`（`type: array`）、非空有序映射 `OrderedMap<string, V>`（`type: object` 且配置 `additionalProperties`）或非空字符串 `string`。支持传入顶层变量、结构体静态字段路径或常量字面量；禁止传入可空类型、固定属性结构体 `TypedDict`、`integer`、`number` 或 `boolean`。 |
| **`with.output`** | `result` | **必填** | 写入已在当前图中声明为 `integer`（或兼容的 `number` / 可空数值类型）的目标顶层变量。 |

### 控制流与运行期求值行为

* **无分支单出边（`node -> target`）**：不修改源对象 `source`，按以下规则计算非负整数长度（$\ge 0$）并写入 `result`：
  * 当 `source` 为列表 `Array<T>` 时：返回列表元素个数；
  * 当 `source` 为有序映射 `OrderedMap<string, V>` 时：返回当前键值对条目个数；
  * 当 `source` 为字符串 `string` 时：返回字符串的 Unicode 码点（Code Points）字符个数。

---

## 二、 YAML 用法示例

```yaml
graph:
  variables:
    total_candidates:
      type: integer

  nodes:
    # 步骤 1：非破坏性地读取列表 ranked_candidates 的元素个数，写入 total_candidates
    measure_candidates:
      use: builtin:length
      with:
        input:
          source: ranked_candidates
        output:
          result: total_candidates

    # 步骤 2：配合 builtin:compare 判断列表是否非空（total_candidates > 0）
    check_has_candidates:
      use: builtin:compare
      with:
        input:
          op: { value: "gt" }
          left: total_candidates
          right: 0

  edges:
    - start -> measure_candidates
    - measure_candidates -> check_has_candidates
    - check_has_candidates.true -> process_candidates
    - check_has_candidates.false -> end.empty_batch
```
