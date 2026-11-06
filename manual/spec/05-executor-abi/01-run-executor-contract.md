# 01. 业务执行器（`run`）标准 ABI 契约规范

在 Axcutor 架构中，`run` 节点代表**任意受函数签名约束的原子业务执行单元**。工作流运行时调度器（Runtime Scheduler）与具体的业务执行器实现（Executor Implementation）完全解耦，通过本规范定义的标准应用二进制/数据接口（Executor ABI Contract）通信。

无论是本地/远程原生函数、Python/JS 脚本沙箱、微服务 HTTP/RPC 调用、数据库查询与事务操作，还是大模型结构化输出（LLM Structured Outputs），只要实现本契约，即可无缝作为 `run` 节点的执行后端。

---

## 一、 执行器调用的抽象函数签名

从运行时调度器的视角看，任何一个 `run` 节点的单次尝试（Attempt）均等价于调用如下异步纯契约函数：

$$\text{ExecuteRun}(\text{RunSpec}, \text{Contract}, \text{InputSnapshot}, \text{AbortSignal}) \longrightarrow \text{Result}\langle \text{ExecutionSuccess}, \text{ExecutionError} \rangle$$

---

## 二、 运行时传入执行器的标准请求载荷（Request Payload）

每次激活 `run` 节点的单次尝试时，运行时向执行器传入以下只读结构体：

| 字段 | 类型 | 契约说明 |
| :--- | :--- | :--- |
| **`run_spec`** | `Any`（由具体执行器解析） | YAML 中 `run` 字段声明的执行体定义（如函数标识符、脚本代码、RPC 配置或指令模板）。 |
| **`input_snapshot`** | `ReadOnlyMap<Identifier, JSONValue>` | **最小权限只读输入快照**：仅包含该节点 `input: [...]` 声明的变量（或顶层 `RootRunNode` 的 `input`）在激活时刻的深拷贝/不可变值。未在 `input` 中声明的图变量严禁出现在此映射中。 |
| **`output_schema`** | `Map<Identifier, TypeSpec>` | 该节点声明的完整 `output` 类型契约表。 |
| **`branches_contract`** | `Map<BranchId, { description: String, output: List<Identifier> }>` | 规范化后的控制分支表。若节点未声明 `branches`，则为空映射 `{}`。 |
| **`abort_signal`** | `CancellationToken` | 当单次尝试触及 `timeout_ms` 或并发兄弟分支触发 `CancelledError` 时，运行时通过该信号通知执行器立即中止底层计算或 I/O。 |

---

## 三、 执行器返回给运行时的标准响应载荷（Response Payload）

执行器完成业务逻辑后，必须向运行时返回以下结构化响应或抛出执行期错误：

### 1. 成功返回结构（`ExecutionSuccess`）

```json
{
  "branch": "optional_chosen_branch_id_or_null",
  "output": {
    "field_a": "...",
    "field_b": 123
  }
}
```

### 2. 运行时边界契约校验（Produces `contract` Exception on Violation）

运行时收到 `ExecutionSuccess` 后，在将其合并入图 `variables` 之前，强制执行以下两项边界校验（任何一项失败均丢弃返回值并抛出可恢复的 **`contract` 异常**）：

1. **分支合法性校验**：
   * 若 `branches_contract` 为空（无分支节点）：`branch` 必须为 `null` 或省略，且预期产出字段集为 $F_{\text{expected}} = \text{dom}(\text{output\_schema})$；
   * 若 `branches_contract` 非空（有分支节点）：`branch` 必须为非空字符串且满足 $\text{branch} \in \text{dom}(\text{branches\_contract})$，此时预期产出字段集为该分支关联的子集 $F_{\text{expected}} = \text{set}(\text{branches\_contract}[\text{branch}].\text{output})$。
2. **输出字段子集与 `TypeSpec` Schema 校验**：
   * `output` 映射必须包含 $F_{\text{expected}}$ 中的每一个字段（$\forall f \in F_{\text{expected}}, f \in \text{dom}(\text{output})$）；
   * 对于每个 $f \in F_{\text{expected}}$，其值 `output[f]` 必须严格通过 `output_schema[f]` 的 JSON Schema 校验（含闭合结构体禁止额外未声明属性、枚举值匹配与非空检查）。
   * 校验通过后，运行时**仅提取 $F_{\text{expected}}$ 内的字段**原子写回所属栈帧。

### 3. 执行器运行期故障映射（Produces `execution` Exception）

当执行器内部发生任何业务或基础设施故障（包括但不限于：业务函数抛出未捕获异常、脚本退出码非零、外部 HTTP/RPC 返回网络错误或 5xx、数据库连接断开、模型 API 调用失败等）时，执行器统一将其封装为 `ExecutionError` 返回给运行时，由运行时映射为标准可恢复异常 **`execution`**，进入节点的 `retry` / `catch` 状态机。
