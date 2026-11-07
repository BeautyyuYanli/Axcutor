# `builtin:array_shift`：列表首元素弹出与剩余切片

在成环回边（`while`）循环中，从列表头部弹出第一个元素 `item` 与剩余切片列表 `rest`，用于实现**命中即停的短路早退遍历**或**动态工作队列**。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的非空强类型列表 `{ type: array, items: T }`（支持顶层变量或结构体字段路径）。 |
| **`with.output`** | `item` | **必填** | 写入当前图符号表中已声明的目标顶层变量 $v_{\text{item}}$，要求 $\Sigma(v_{\text{item}}) \equiv T$，且不可与 `rest` 同名。 |
| **`with.output`** | `rest` | **必填** | 写入当前图符号表中已声明的目标顶层变量 $v_{\text{rest}}$，要求 $\Sigma(v_{\text{rest}}) \equiv \{ \texttt{type}: \texttt{array}, \texttt{items}: T \}$。可与 `source` 绑定同一个顶层列表变量名，以推进队列切片。 |

### 控制流分支行为、不可变切片与反定值（Kill）语义

* **`.some` 分支（运行时 `len(source) > 0`，关联 `output: [item, rest]`）**：将 `source[0]` 写入 `item`，并以纯函数式不可变方式（Copy-on-Write）构造剩余切片的新列表副本 `source[1:]` 写入 `rest`（不原地篡改输入快照引用）；下游路径上 `item` 与 `rest` 均获得定值。
* **`.empty` 分支（运行时 `len(source) == 0`，关联 `output: []`，反定值杀死集 `KillVars = {item}`）**：不写入 `item` 与 `rest`；在编译期必达定值分析中从入口定值集移除 `item`（$Def_{\text{out}}(N.\text{empty}) = Def_{\text{in}}(N) \setminus \{item\}$），并在运行期进入 `.empty` 分支时将当前栈帧中的 `item` 置为未绑定（Unbound）。若在 `.empty` 下游重新定值前读取 `item` 将在编译期报错；同时允许同一张图中前后多个独立的 `array_shift` 循环合法复用同一个迭代变量名 `item`。

---

## 二、 YAML 用法示例

```yaml
graph:
  # 显式声明弹出的元素变量类型（remaining_endpoints 已在顶层 input 中声明）
  variables:
    current_endpoint:
      type: string

  nodes:
    # 弹出 remaining_endpoints 的首元素至 current_endpoint，并将剩余切片新副本写回 remaining_endpoints
    pop_next_endpoint:
      use: builtin:array_shift
      with:
        input:
          source: remaining_endpoints
        output:
          item: current_endpoint
          rest: remaining_endpoints

  edges:
    - start -> pop_next_endpoint
    # 队列非空时尝试当前节点；成功则短路退出，失败则回边继续弹出下一项
    - pop_next_endpoint.some -> try_request
    - try_request.ok -> end.success
    - try_request.failed -> pop_next_endpoint
    # 队列耗尽时退出循环（current_endpoint 在 .empty 分支上被反定值置为未绑定）
    - pop_next_endpoint.empty -> end.all_failed
```
