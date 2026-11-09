# 01. 业务执行器（`run`）标准 ABI 契约规范

在 Axcutor 架构中，`run` 节点代表**受函数签名约束的原子业务执行单元**。工作流运行时调度器（Runtime Scheduler）与具体的业务执行器实现（Executor Implementation）解耦，通过本规范定义的标准应用数据接口（Executor ABI Contract）通信。

无论是内建的进程内受信任 Python 内联执行器（`executor: "builtin"`，详见 [`02-builtin-python-executor.md`](./02-builtin-python-executor.md)）、基于工作流包同构的模块级 Python 执行器（`executor: "module"`，详见 [`03-module-python-executor.md`](./03-module-python-executor.md)）、工作流内协作式等待执行器（`executor: "signal"`，详见 [`04-signal-cooperative-executor.md`](./04-signal-cooperative-executor.md)），还是在此基础上构建或独立注册的扩展执行器（如隔离沙箱、微服务 HTTP/RPC 调用、数据库事务操作或大模型结构化输出），均遵循统一的输出与控制分支契约。

---

## 一、 执行器调用的抽象函数签名

从运行时调度器的视角看，Activity 执行器（`builtin`、`module` 及扩展执行器）的单次尝试（Attempt）等价于调用如下异步契约函数（`signal` 执行器在工作流内部挂起并通过外部信号恢复，其等待与恢复协议见 [`04-signal-cooperative-executor.md`](./04-signal-cooperative-executor.md)）：

$$\text{ExecuteRun}(\text{RunSpec}, \text{InputSchema}, \text{InputSnapshot}, \text{OutputSchema}, \text{BranchesContract}, \text{AbortSignal}) \longrightarrow \text{Result}\langle \text{ExecutionSuccess}, \text{ExecutionError} \rangle$$

---

## 二、 运行时传入执行器的标准请求载荷（Request Payload）

每次激活 Activity 执行器（`builtin`、`module` 或扩展执行器）的单次尝试时，运行时根据 `run_spec.executor` 路由并传入以下只读请求载荷：

| 字段 | 类型 | 契约说明 |
| :--- | :--- | :--- |
| **`workflow_id`** | `PackagePath` 或 `__main__` | 当前节点所属工作流标识，嵌套内联图与迭代沿用所属包。`module` 据此解析包内模块；Worker 的 Python 根映射不由提交者指定。 |
| **`run_spec`** | `RunSpec`（含鉴别键 `executor: Identifier`） | 节点声明的结构化执行体定义（如 `{ executor: "builtin", content: "..." }`、`{ executor: "module", content: ".<local_path>" }` 或其他扩展执行器配置对象）。 |
| **`input_schema`** | `Map<RestrictedIdentifier, TypeSpec>` | **输入变量的静态类型契约表**：由编译器在阶段 2 符号表解析期间从当前作用域符号表提取并导出的归一化 JSON Schema `TypeSpec` 映射（即 $\Sigma|_{N.\text{input}}$ 或 `root.input`），供执行器在边界生成强类型输入模型或执行静态/动态入参投影。 |
| **`input_snapshot`** | `ReadOnlyMap<RestrictedIdentifier, JSONValue>` | **最小权限只读输入快照**：仅包含该节点 `input: [...]` 声明的变量（或顶层 `RootRunNode` 的 `input`）在激活时刻的**深拷贝/不可变值（Deep-Frozen / Immutable Value）**。未在 `input` 中声明的图变量不出现在此映射中；执行器在本地内存或进程内对该快照的任何就地修改（In-place Mutation）均与运行时所属栈帧的 `variables` 物理隔离，不得产生任何跨节点副作用。 |
| **`output_schema`** | `Map<RestrictedIdentifier, TypeSpec>` | 该节点声明的完整单次输出类型契约表（归一化的 JSON Schema `TypeSpec` 映射）。 |
| **`branches_contract`** | `Map<BranchId, { description: String, output: List<RestrictedIdentifier> }>` | 规范化后的控制分支表。若节点未声明 `branches`，则为空映射 `{}`。 |
| **`attempt_timeout_ms`** | `PositiveInt` | 当前单次尝试的有效超时预算 $A$（毫秒），取节点显式 `timeout_ms` 或运行选项 `activity_timeout_ms`，用于 Activity 进程内建立本地单调时钟 `deadline`。 |
| **`heartbeat_interval_ms`** | `PositiveInt` | Activity 进程内发送应用心跳的本地间隔 $I$（毫秒），取运行选项显式值或由 `heartbeat_timeout_ms` 派生的 $\max(1, \lfloor T / 3 \rfloor)$。 |
| **`abort_signal`** | `CancellationToken` | **进程内协作式取消信号**：由 Activity 尝试在本地创建并传入执行器函数（不跨网络序列化）。当单次尝试触及本地 `deadline`（`reason = "timeout"`）或收到工作流/宿主取消信号（`reason = "cancelled"`）时置位，通知执行器中止执行并释放外部资源。 |

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

业务返回形状不变。Activity 与独立 Child 在内部统一使用 `result/resources` 外壳，先完成资源接管，再提交或映射完整业务输出。根宿主仍返回原有业务形状，详见[资源交接](./05-resource-lifecycle.md)。

运行时收到 Activity 的 `ExecutionSuccess` 后，在将其合并入图 `variables` 之前，在 Activity 出口与工作流接收边界均执行以下两项边界校验（任一项失败，或执行器在构造返回值期间显式报告契约校验失败，均丢弃返回值并产生可恢复的 **`contract` 异常**；对于 `executor: "signal"`，相同的两项契约校验在外部信号接收边界同步执行，校验失败时直接以 `422 signal_validation_failed` 拒绝外部请求并保持节点等待状态，详见 [`04-signal-cooperative-executor.md`](./04-signal-cooperative-executor.md)）：

1. **分支合法性校验**：
   * 若 `branches_contract` 为空（无分支节点）：`branch` 必须为 `null` 或省略，且预期产出字段集为 $F_{\text{expected}} = \text{dom}(\text{output\_schema})$；
   * 若 `branches_contract` 非空（有分支节点）：`branch` 必须为非空字符串且满足 $\text{branch} \in \text{dom}(\text{branches\_contract})$，此时预期产出字段集为该分支关联的子集 $F_{\text{expected}} = \text{set}(\text{branches\_contract}[\text{branch}].\text{output})$。
2. **输出字段子集与 `TypeSpec` Schema 校验**：
   * **必选字段完备性**：`output` 映射必须包含 $F_{\text{expected}}$ 中的每一个字段（$\forall f \in F_{\text{expected}}, f \in \text{dom}(\text{output})$）；若缺失任一预期字段，立即判定契约违约；
   * **深层 `TypeSpec` Schema 校验**：对于每个 $f \in F_{\text{expected}}$，其值 `output[f]` 必须通过 `output_schema[f]` 的严格类型与结构校验（包括闭合结构体 `{ ... }` 内部禁止出现未声明属性 `additionalProperties: false`、枚举值匹配与非空检查）；
   * **顶层额外字段过滤与原子写回边界**：对于顶层 `output` 映射中超出 $F_{\text{expected}}$ 的额外字段（即 $\text{dom}(\text{output}) \setminus F_{\text{expected}}$，如非当前激活分支关联的字段或执行器附带的冗余顶层键），运行时在边界处执行**静默过滤（白名单投影）**——既不对这些额外顶层字段执行 `TypeSpec` 校验，也不将其写入图状态；校验通过后，运行时**仅提取 $F_{\text{expected}}$ 内的字段（$\text{output}|_{F_{\text{expected}}}$）**并生成不可变深拷贝，原子写回所属栈帧。

### 3. 执行器运行期故障映射（Produces `execution` Exception）

当执行器内部发生业务或基础设施故障（包括业务函数抛出未捕获的非契约异常、脚本退出码非零、外部 HTTP/RPC 返回网络错误或 5xx、数据库连接断开、模型 API 调用失败等）且当前尝试尚未被 `abort_signal` 取消时，执行器统一将其封装为 `ExecutionError` 返回给运行时，由运行时映射为标准可恢复异常 **`execution`**，进入节点的 `retry` / `catch` 状态机。

### 4. 心跳、本地 Deadline、`abort_signal` 取消响应与迟到结果丢弃（Cancellation & Late-Result Dropping）

Activity 单次尝试同时维护业务协程、本地单调时钟 `deadline`（预算 `attempt_timeout_ms`）、取消监听协程以及周期为 `heartbeat_interval_ms` 的心跳协程（心跳负责向后端上报存活并接收取消通知；若心跳协程自身发生基础设施异常，则中止尝试并转化为不可恢复的 `EngineError`）。当触发 `abort_signal` 时，执行器与运行时必须遵循以下契约：

1. **执行器资源清理义务与任务排空**：`abort_signal` 置位后，业务应尽快中止 I/O、子进程和计算。Activity 取消并排空业务及派生任务，本轮排空与清理共用 30 秒预算；未实际排空的本地使用记录继续保留，排空后驱动 pending 清理。Workflow 的旧调度授权在有界收尾后结束，不无限等待失联任务。析构失败保留责任，不替换业务异常。
2. **提交前单调时钟复查与迟到结果静默丢弃（Late-Result Dropping）**：
   * `abort_signal` 触发的瞬间，该次尝试在运行时内部的生命周期即告终结（因超时触发者产生 `timeout` 异常并按需转入 `retry` / `catch` 状态机；因 `CancelledError` 触发者推进取消收敛流程）；
   * 即使业务协程在同步编译、序列化或 `validate_run_contract` 契约校验期间未让出事件循环而赶在 `deadline` 协程被调度前产出返回值，Activity 在最终返回前仍会复查本地单调时钟（`loop.time() >= expires_at`），一旦超期立即将 `abort_signal` 置为 `"timeout"` 并丢弃该迟到成功结果；
   * 尝试中止后执行器异步返回的任何迟到 `ExecutionSuccess` 或 `ExecutionError` 均在 `cancel_and_drain` 阶段被静默丢弃——既不会写入图 `variables`，也不会再次触发 `contract` / `execution` 异常或重复进入该节点的 `retry` / `catch` 状态机。
