# 01. 业务执行器（`run`）标准 ABI 契约规范

在 Axcutor 架构中，`run` 节点代表**受函数签名约束的原子业务执行单元**。工作流运行时调度器（Runtime Scheduler）与具体的业务执行器实现（Executor Implementation）解耦，通过本规范定义的标准应用数据接口（Executor ABI Contract）通信。

无论是内建的进程内受信任 Python 内联执行器（`executor: "builtin"`，详见 [`02-builtin-python-executor.md`](./02-builtin-python-executor.md)）、基于工作流包同构的模块级 Python 执行器（`executor: "module"`，详见 [`03-module-python-executor.md`](./03-module-python-executor.md)），还是在此基础上构建或独立注册的扩展执行器（如隔离沙箱、微服务 HTTP/RPC 调用、数据库事务操作或大模型结构化输出），只要遵循本契约，即可作为 `run` 节点的执行后端。

---

## 一、 执行器调用的抽象函数签名

从运行时调度器的视角看，Activity 执行器的单次尝试（Attempt）等价于调用如下异步契约函数；`signal` 的纯等待协议见第四节：

$$\text{ExecuteRun}(\text{RunSpec}, \text{InputSchema}, \text{InputSnapshot}, \text{OutputSchema}, \text{BranchesContract}, \text{AbortSignal}) \longrightarrow \text{Result}\langle \text{ExecutionSuccess}, \text{ExecutionError} \rangle$$

---

## 二、 运行时传入执行器的标准请求载荷（Request Payload）

每次激活 Activity 执行器（`builtin`、`module` 或扩展执行器）的单次尝试时，运行时根据 `run_spec.executor` 路由并传入以下只读请求载荷；`signal` 使用第四节的 Update 协议：

| 字段 | 类型 | 契约说明 |
| :--- | :--- | :--- |
| **`workflow_id`** | `PackagePath` 或 `__main__` | 当前节点所属工作流标识，嵌套内联图与迭代沿用所属包。`module` 据此解析包内模块；Worker 的 Python 根映射不由提交者指定。 |
| **`run_spec`** | `RunSpec`（含鉴别键 `executor: Identifier`） | 节点声明的结构化执行体定义（如 `{ executor: "builtin", content: "..." }`、`{ executor: "module", content: ".<local_path>" }` 或其他扩展执行器配置对象）。 |
| **`input_schema`** | `Map<RestrictedIdentifier, TypeSpec>` | **输入变量的静态类型契约表**：由编译器在阶段 2 符号表解析期间从当前作用域符号表提取并导出的归一化 JSON Schema `TypeSpec` 映射（即 $\Sigma|_{N.\text{input}}$ 或 `root.input`），供执行器在边界生成强类型输入模型或执行静态/动态入参投影。 |
| **`input_snapshot`** | `ReadOnlyMap<RestrictedIdentifier, JSONValue>` | **最小权限只读输入快照**：仅包含该节点 `input: [...]` 声明的变量（或顶层 `RootRunNode` 的 `input`）在激活时刻的**深拷贝/不可变值（Deep-Frozen / Immutable Value）**。未在 `input` 中声明的图变量不出现在此映射中；执行器在本地内存或进程内对该快照的任何就地修改（In-place Mutation）均与运行时所属栈帧的 `variables` 物理隔离，不得产生任何跨节点副作用。 |
| **`output_schema`** | `Map<RestrictedIdentifier, TypeSpec>` | 该节点声明的完整单次输出类型契约表（归一化的 JSON Schema `TypeSpec` 映射）。 |
| **`branches_contract`** | `Map<BranchId, { description: String, output: List<RestrictedIdentifier> }>` | 规范化后的控制分支表。若节点未声明 `branches`，则为空映射 `{}`。 |
| **`abort_signal`** | `CancellationToken` | **协作式取消信号**：当单次尝试触及 `timeout_ms` 或并发兄弟分支触发 `CancelledError` 时，运行时通过该信号通知执行器中止执行。执行器必须监听该信号并在触发时尽快中止底层计算或 I/O、释放外部资源（如关闭网络连接、终止子进程、回滚未提交事务）。 |

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

运行时收到 Activity 的 `ExecutionSuccess` 后，在将其合并入图 `variables` 之前，执行以下两项边界校验（任一项失败，或执行器在构造返回值期间显式报告契约校验失败，均丢弃返回值并产生可恢复的 **`contract` 异常**）：

1. **分支合法性校验**：
   * 若 `branches_contract` 为空（无分支节点）：`branch` 必须为 `null` 或省略，且预期产出字段集为 $F_{\text{expected}} = \text{dom}(\text{output\_schema})$；
   * 若 `branches_contract` 非空（有分支节点）：`branch` 必须为非空字符串且满足 $\text{branch} \in \text{dom}(\text{branches\_contract})$，此时预期产出字段集为该分支关联的子集 $F_{\text{expected}} = \text{set}(\text{branches\_contract}[\text{branch}].\text{output})$。
2. **输出字段子集与 `TypeSpec` Schema 校验**：
   * **必选字段完备性**：`output` 映射必须包含 $F_{\text{expected}}$ 中的每一个字段（$\forall f \in F_{\text{expected}}, f \in \text{dom}(\text{output})$）；若缺失任一预期字段，立即抛出 `contract` 异常；
   * **深层 `TypeSpec` Schema 校验**：对于每个 $f \in F_{\text{expected}}$，其值 `output[f]` 必须通过 `output_schema[f]` 的严格类型与结构校验（包括闭合结构体 `{ ... }` 内部禁止出现未声明属性 `additionalProperties: false`、枚举值匹配与非空检查）；
   * **顶层额外字段过滤与原子写回边界**：对于顶层 `output` 映射中超出 $F_{\text{expected}}$ 的额外字段（即 $\text{dom}(\text{output}) \setminus F_{\text{expected}}$，如非当前激活分支关联的字段或执行器附带的冗余顶层键），运行时在边界处执行**静默过滤（白名单投影）**——既不对这些额外顶层字段执行 `TypeSpec` 校验，也不将其写入图状态；校验通过后，运行时**仅提取 $F_{\text{expected}}$ 内的字段（$\text{output}|_{F_{\text{expected}}}$）**并生成不可变深拷贝，原子写回所属栈帧。

### 3. 执行器运行期故障映射（Produces `execution` Exception）

当执行器内部发生业务或基础设施故障（包括业务函数抛出未捕获的非契约异常、脚本退出码非零、外部 HTTP/RPC 返回网络错误或 5xx、数据库连接断开、模型 API 调用失败等）且当前尝试尚未被 `abort_signal` 取消时，执行器统一将其封装为 `ExecutionError` 返回给运行时，由运行时映射为标准可恢复异常 **`execution`**，进入节点的 `retry` / `catch` 状态机。

### 4. `abort_signal` 取消响应契约与迟到结果丢弃（Cancellation & Late-Result Dropping）

当运行时触发某次尝试（Attempt）的 `abort_signal`（原因包括单次尝试触及 `timeout_ms` 超时边界，或所属节点/子图收到并发汇合产生的 `CancelledError`）时，执行器与运行时必须遵循以下取消契约：

1. **执行器资源清理义务**：`abort_signal` 一旦置位，执行器应立即中止正在进行的底层 I/O（如主动断开 HTTP/RPC 连接、取消数据库查询游标）、终止沙箱子进程或停止本地计算，并确保释放已占用的外部句柄与锁资源。
2. **迟到结果的静默丢弃（Late-Result Dropping）**：`abort_signal` 触发的瞬间，该次尝试在运行时内部的生命周期即告终结（因 `timeout_ms` 触发者由运行时直接生成 `timeout` 异常并按需转入 `retry` / `catch` 状态机；因 `CancelledError` 触发者直接推进取消收敛流程）。此后执行器异步返回的任何迟到 `ExecutionSuccess` 或 `ExecutionError`（包括因取消操作本身引发的底层连接中断错误）**均被运行时静默丢弃**——既不会写入图 `variables`，也不会再次触发 `contract` / `execution` 异常或重复进入该节点的 `retry` / `catch` 状态机。

---

## 四、`signal` 协作式等待与 Update 恢复协议

`run: { executor: signal }` 是只包含鉴别键的闭合执行体。在解释器 Workflow 内通过 `wait_condition` 等待外部消息，不调度 Activity、不占 Activity Worker 槽位、不发送心跳。准备通知和后置处理使用前后相邻的普通节点；`axcutor contracts` 跳过 `signal`，不生成 Python 文件。

### 等待槽位与所有权

每次尝试在 Workflow 内存表中以规范拓扑地址注册一个槽位，保存所属帧引用、输出 Schema、分支契约与等待状态。根单步节点使用帧地址，图内节点使用 `<frame>/<node>`，直接迭代使用 `<frame>/<node>[index]`。同一帧可同时管辖多个 Fork-Join 挂起点。

当前 Python SDK 在首个 Workflow Task 不产生命令时，可能在冷重放恢复槽位之前执行外部 Update Validator，见 [SDK issue #2080](https://github.com/temporalio/sdk-python/issues/2080)。运行时为此在必要时启动一个名义时长为 1 毫秒的初始化 Workflow Timer，以建立持久化调度边界；信号被认领后立即恢复 CFG 推进，不等待该 Timer 触发。Temporal 不保证亚秒 Timer 的精确触发延迟，该时长不构成业务等待或恢复延迟承诺。

初始化 Timer 由当前 Workflow Run 持有并在退出时取消、回收；循环、重试与并发等待共享该边界，整个 Run 至多创建一个，各 Child Workflow 独立管理各自 Run 的边界。已成功返回的 Activity、已经启动的 Child Workflow、已经触发的期限或已接受的信号 Update 可以提供持久化边界；等待协程自身或同一 Run 内祖先的业务期限也可提供边界，无需另加初始化 Timer。取消其他并发分支的期限不作为当前等待的依据。必要的初始化 Timer 会额外产生 Timer Started 和 Timer Fired 或 Timer Canceled 等历史事件，不占 Activity 槽位、不额外计步，也不限制无限期等待。等待状态和响应仍由 Temporal 重放重建，不另存业务检查点。

快照的 `waiting: list[str]` 按注册顺序给出当前帧直接管辖的待认领地址，未挂起时为 `[]`。宿主递归合并 Child Workflow 时，对帧地址及每个等待地址执行相同的逻辑前缀替换。外部调用者直接使用 `waiting` 元素投递，无需从游标推导地址。

### 无损 Update 边界

`deliver_signal(request_json: str) -> str` 的输入与确认响应均使用运行时 codec 的 JSON 文本；数值和对象键序不得经普通浮点 JSON 转换。请求结构为：

```json
{"address":"__main__/decision","branch":"approved","output":{"reviewer":"alice"},"step":2}
```

同步 Validator 只读检查请求结构、地址是否待认领、可选 `step` 是否等于所属帧当前步数，再调用 `validate_run_contract` 校验并投影 `(branch, output)`。地址或步数不匹配以 `SignalNotWaitingError` 拒绝；契约错误以 `SignalValidationError` 拒绝；内核 `RuntimeError` 包装为 `EngineError`。拒绝不修改帧或槽位，不触发节点 `contract`，也不消耗重试次数。Temporal 不记录该请求的 Update Accepted/Completed 事件；服务器仍可能记录处理拒绝请求的 Workflow Task 调度事件。

同步 Handler 重新执行同一校验以支持重放，随后一次性写入规范化响应、标记已投递并移除地址。一个槽位只能认领一次；完成确认表示信号已被认领，后续控制流及并发屏障仍由原执行协程负责。

### 超时、重试与取消

`timeout_ms` 限制单次等待，省略时无限期等待，不使用 `activity_timeout_ms` 默认值。仅可能产生 `timeout` 可恢复异常；`retry` 默认选择 `[timeout]`，`catch` 的 `"*"` 也只对 `{timeout}` 求补。每次重试重新注册槽位并重置定时器，复用原输入快照；等待、拒绝和内部重试均不额外计步。

若 Update 已同步认领槽位，同轮次就绪的尝试定时器不得把该成功改判为超时。若尚未认领，取消等待并移除槽位，后续投递返回冲突。所有成功、超时及取消路径均清理槽位；Workflow 取消和 Fail-Fast 仍向上展开。`for_each` 等待期间持有并发许可，输出仍暂存在槽位中，所有成员完成后才通过屏障提交。

宿主按路径段边界递归路由 Child Workflow，固定每层 run 后发送 Update，再刷新根执行。HTTP/CLI 字段、状态码及投递结果不确定时的查询方式见 [Execution API](../../docs/reference/04-execution-http-api.md)。长期高轮次交互宜分段调用子工作流，避免单个 Temporal Run 的历史与 Update 容量持续增长。
