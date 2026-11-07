# `builtin:assign`：确定性变量赋值、字段提取与常量绑定

在不调用外部 `run` 执行器的前提下，将当前图作用域内的顶层变量、强类型结构体（`TypedDict`）的静态字段路径投影（`FieldPathExpr`）或常量字面量绑定写入目标顶层变量 `value`。

常见适用场景包括：
1. **初始化循环工作队列**：在进入 `while` 成环回边循环前，将输入结构体中的列表字段（如 `request.items`）拷贝至顶层工作变量 `working_queue`，供 `builtin:array_shift` 原地切片推进；
2. **分支合流处的常量或缓存定值**：在条件分支或 `catch` 恢复分支上，直接将目标变量赋值为静态常量（如 `{ value: "Skipped" }`）或已有变量，以满足下游汇合节点的全路径必达定值要求；
3. **提取嵌套字段作为图顶层输出**：将内部节点产出的结构体嵌套字段（如 `user_info.email`）直接绑定至宿主图 `output` 要求的顶层变量。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 支持绑定已定值的顶层变量、非空 `TypedDict` 的静态字段路径（`FieldPathExpr`），或常量字面量（数值、布尔、`null` 或 `{ value: <literal> }` 包装字面量）。 |
| **`with.output`** | `value` | **必填** | 写入目标顶层变量 $v_{\text{out}}$。类型按以下规则推导与校验：<br>• 当 `source` 为变量或字段路径 $E$ 时：自动推导为 $T_{\text{src}} = \text{TypeOf}(\Sigma, E)$（若 $v_{\text{out}}$ 已在符号表中存在，须满足静态类型等价 $\Sigma(v_{\text{out}}) \equiv T_{\text{src}}$）；<br>• 当 `source` 为常量字面量 $L$ 且 $v_{\text{out}} \in \text{dom}(\Sigma)$ 时：校验实例兼容性 $L \models \Sigma(v_{\text{out}})$；<br>• 当 `source` 为常量字面量 $L$ 且 $v_{\text{out}} \notin \text{dom}(\Sigma)$ 时：$L$ 必须为非空基础标量字面量（`string` / `integer` / `number` / `boolean`），自动推导为对应基础标量类型。 |

### 控制流与运行期求值行为

* **无分支单出边（`node -> target`）**：读取 `with.input.source` 的运行期值 $x = \text{Eval}(\rho, \text{source})$ 并绑定至当前栈帧顶层变量 $\rho(v_{\text{out}}) \leftarrow x$，在下游路径上提供 `value` 的定值（`output_subset = {v_out}`）。

---

## 二、 YAML 用法示例

```yaml
nodes:
  # 场景 1：将输入结构体的嵌套字段 request.items 拷贝至顶层变量 working_queue，供后续循环消费
  init_working_queue:
    use: builtin:assign
    with:
      input:
        source: request.items
      output:
        value: working_queue

  # 场景 2：在跳过分支上将汇合变量 audit_report 直接定值为常量字符串 "Skipped"
  assign_skipped_report:
    use: builtin:assign
    with:
      input:
        source: { value: "Skipped" }
      output:
        value: audit_report

  # 场景 3：提取结构体嵌套字段 user_profile.contact.email 写入图顶层输出变量 primary_email
  extract_primary_email:
    use: builtin:assign
    with:
      input:
        source: user_profile.contact.email
      output:
        value: primary_email

edges:
  - start -> init_working_queue
  - init_working_queue -> check_need_audit
  - check_need_audit.true -> run_full_audit
  - check_need_audit.false -> assign_skipped_report
  - run_full_audit -> extract_primary_email
  - assign_skipped_report -> extract_primary_email
  - extract_primary_email -> end
```
