# `builtin:array_shift`：列表首元素弹出与剩余切片

在成环回边（`while`）循环中，从列表头部弹出第一个元素 `item` 与剩余切片列表 `rest`，用于实现**命中即停的短路早退遍历**或**动态工作队列**。

---

## 一、 参数与分支契约

| 方向 | 参数名 | 必填/可选 | 类型契约与静态约束 |
| :--- | :--- | :--- | :--- |
| **`with.input`** | `source` | **必填** | 绑定已定值的强类型列表（即 `type: array` 且明确声明了元素类型 `items`；支持顶层变量或结构体字段路径）。 |
| **`with.output`** | `item` | **必填** | 写入当前图中已声明的顶层变量；其声明类型必须兼容 `source` 的元素类型，且不可与 `rest` 绑定同一个变量名。 |
| **`with.output`** | `rest` | **必填** | 写入当前图中已声明的顶层列表变量；其声明类型必须兼容 `source` 的列表类型。可与 `source` 绑定同一个顶层列表变量名，用于在循环中推进队列切片。 |

### 控制流分支行为与不可变切片语义

* **`.some` 分支（列表非空，产出 `item` 与 `rest`）**：当 `source` 至少包含一个元素时走此分支。节点将首元素 `source[0]` 写入 `item`，并以不可变方式构造除首元素外的剩余切片副本 `source[1:]` 写入 `rest`（不会原地修改原列表引用）；在该分支下游路径上，`item` 与 `rest` 均处于已定值状态，可直接读取。
* **`.empty` 分支（列表为空，不产出新值并重置 `item` 定值状态）**：当 `source` 为空列表时走此分支。节点不会写入 `item` 与 `rest`，同时会将 `item` 重置为**未定值（未绑定）**状态，防止下游误读上一轮循环残留的旧元素值。若在 `.empty` 分支下游未重新赋值就读取 `item`，静态检查将直接报错；这一机制也允许同一张图中前后多个独立的 `array_shift` 循环安全地复用同一个 `item` 迭代变量名。

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
    # 队列耗尽时退出循环（current_endpoint 在 .empty 分支上已重置为未定值状态）
    - pop_next_endpoint.empty -> end.all_failed
```
