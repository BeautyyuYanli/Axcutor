# 04. 协作式等待执行器（`executor: "signal"`）与外部恢复协议规范

本规范定义 Axcutor 标准内置协作式等待执行器 `run: { executor: "signal" }` 的闭合配置文法、运行期等待槽位与拓扑寻址模型、只读验证与原子认领恢复协议、单次尝试超时与重试/取消状态机，以及跨子工作流（Child Workflow）的透明信号路由契约。

---

## 一、 执行定位与闭合配置边界

1. **纯等待工作流原语（Pure Cooperative Suspension）**：
   * 与承载计算或外部 I/O 的 Python 执行器（[`executor: "builtin"`](./02-builtin-python-executor.md)、[`executor: "module"`](./03-module-python-executor.md)）不同，`executor: "signal"` 不运行任何业务函数或宿主脚本，也不调度底层 Activity 或产生心跳。
   * 节点激活时，运行时直接在解释器工作流内部挂起当前执行路径，等待外部调用方提交满足该节点 `(output, branches)` 契约的恢复信号。挂起前后的外部通知发送或唤醒后的业务处理，由控制流图（CFG）中前后相邻的 `builtin`、`module` 或子工作流节点承担。
2. **闭合配置与工具链边界**：
   * `SignalRunSpec` 必须且只能包含鉴别键 `executor: "signal"`，禁止声明 `content` 或任何额外字段（否则编译期报 `schema.unknown_field`）。
   * 匿名入口工作流（`__main__`）与具名工作流包均可使用 `executor: "signal"`，顶层单步根节点（`RootRunNode`）与图内动作节点（`ActionNode`）均支持挂载。
   * 契约生成命令 `axcutor contracts` 仅针对 `executor: "module"` 生成 `.py` 契约桩文件，遇到 `executor: "signal"` 节点时直接跳过。

---

## 二、 等待槽位生命周期与规范拓扑地址（Canonical Waiting Address）

### 1. 激活实例的规范拓扑地址

当控制流激活 `executor: "signal"` 节点的单次尝试（Attempt）时，运行时以该激活实例的唯一规范拓扑地址 $\text{addr}$ 在当前工作流实例的挂起注册表中登记一个待认领槽位（Status = `pending`）：

| 节点形态 | 所属栈帧类型 | 规范等待地址 $\text{addr}$ | 示例 |
| :--- | :--- | :--- | :--- |
| **顶层单步根节点（`RootRunNode`）** | `FunctionFrame` | 所属函数帧地址 $\text{frame.address}$ | `"__main__"` 或 `"billing.approve"` |
| **图内普通或 Fork-Join 节点（`ActionNode`）** | `FunctionFrame` / `BlockFrame` | $\texttt{"}\langle\text{frame.address}\rangle\texttt{/}\langle\text{node\_id}\rangle\texttt{"}$ | `"__main__/await_decision"` |
| **挂载 `for_each` 的图内节点单次迭代** | `IterationFrame` | 所属迭代帧地址 $\texttt{"}\langle\text{parent\_frame.address}\rangle\texttt{/}\langle\text{node\_id}\rangle\texttt{[}\langle\text{index}\rangle\texttt{]}"}$ | `"__main__/batch_review[0]"` |

* **同帧多并发挂起点区分**：在 Fork-Join 并发组 `-> [w_1, w_2] ->` 中，多个并发成员共享同一个父图栈帧（此时父帧 `cursor` 停留在分叉前的前驱节点上），但各并发 `signal` 成员分别以各自的 $\texttt{"}\langle\text{frame.address}\rangle\texttt{/}\langle w_i \rangle\texttt{"}$ 独立注册等待槽位。
* **根节点与同名图内节点区分**：当顶层 `RootRunNode` 使用 `signal` 时，帧 `cursor` 为 `"run"` 且等待地址为 $\text{frame.address}$（如 `"__main__"`）；当 `RootGraphNode` 图内存在名为 `"run"` 的子节点使用 `signal` 时，帧 `cursor` 同样为 `"run"`，但等待地址为 $\texttt{"}\langle\text{frame.address}\rangle\texttt{/run"}$（如 `"__main__/run"`）。

### 2. 运行期快照中的 `Frame.waiting` 暴露规则

在运行期状态快照中，每个栈帧 `Frame` 包含有序字符串列表字段 **`waiting: List<String>`**：

* `frame.waiting` 按槽位注册顺序精确列出**当前栈帧直接管辖**且处于 `pending` 状态的规范等待地址 $\text{addr}$；当前帧无挂起 `signal` 节点时为空列表 `[]`。
* 外部调用方直接读取 `frame.waiting` 中的元素即可作为恢复请求的目标 `address`，并可结合同一栈帧的 `frame.steps`（用于步数乐观锁校验）与 `frame.variables`（读取当前挂起上下文变量），无需根据 `frame.kind` 或 `frame.cursor` 推导地址。

---

## 三、 外部信号恢复协议：只读契约校验与原子认领

外部恢复请求面向指定执行与目标等待地址提交以下无损 JSON 载荷：

```json
{
  "address": "__main__/await_decision",
  "branch": "approved",
  "output": {
    "approved_by": "alice"
  },
  "step": 2
}
```

运行时将信号投递划分为**阶段 A（只读验证阶段）**与**阶段 B（原子认领与唤醒阶段）**，确保非法外部请求绝不破坏工作流挂起状态：

### 1. 阶段 A：只读验证阶段（Read-Only Validation Phase）

在向工作流持久化事件历史写入接受记录之前，验证器对请求执行纯只读同步检查：

1. **请求结构预检**：
   * `address` 必须为非空字符串；
   * `output` 必须为 JSON 对象（映射）；宿主入口缺省时默认归一为空对象 `{}`；
   * `branch` 若提供，必须为非空字符串或 `null`；
   * `step` 若提供，必须为正整数（$\text{step} \ge 1$）或 `null`；
   * 顶层禁止出现除 `address`、`branch`、`output`、`step` 以外的未知字段。
2. **挂起状态与可选步数乐观锁（`step` CAS）核验**：
   * 检查目标 `address` 当前是否存在处于 `pending` 状态的等待槽位；若目标节点尚未到达、已完成、已超时或已取消，立即拒绝并返回冲突错误 **`signal_not_waiting`（HTTP `409 Conflict`）**；
   * 若请求提供了正整数 `step`，核对目标槽位所属栈帧的当前累计步数是否满足 $\text{waiter.frame.steps} == \text{step}$；若不匹配（例如在成环回边循环中工作流已被上一轮信号唤醒并再次停在同一 `address` 的新一轮等待上），立即拒绝并返回 **`signal_not_waiting`（HTTP `409 Conflict`）**。
3. **节点输出、外部句柄信封与在途删除冲突校验（`validate_external_output`）**：
   * 构造候选响应 `{"branch": branch, "output": output}`，执行与 [`01-run-executor-contract.md`](./01-run-executor-contract.md) 第三节第 2 小节同构的**分支合法性校验**、**必填输出字段完备性校验**、**深层外部 `TypeSpec` 兼容性校验（$\models_{\text{ext}, \text{bundle.resources}}$）**与**顶层多余字段白名单过滤**：
     * 非 `handle` 位置遵循标准字面量校验；
     * 非 `null` 的 `handle` 位置必须为闭合外部信封 `{"$handle": "<kind>", "locator": <value>}`，要求对应资源已在 `bundle.resources` 中显式声明公开 `locator` 且 `<value>` 满足该纯值 Schema；
     * **在途外部删除冲突拦截**：若某公开句柄定位符 `(kind, locator)` 在当前工作流实例的 `WorkflowResourceRegistry` 中正处于待执行外部实体删除状态（归工作流所有且已转入 `pending_dispose`），同样作为契约冲突拒绝，防止与在途物理删除产生并发竞态；
   * 若违反上述任一契约条件，立即在边界拒绝请求并返回 **`signal_validation_failed`（HTTP `422 Unprocessable Entity`）**（注：静态值校验不隐含跨租户访问授权，资源归属核验由宿主控制面或业务签名定位符协议负责）。

> **非法信号零状态污染不变量**：外部提交的信号属于不可信外部请求而非内部执行器违约。当阶段 A 因任何原因拒绝请求时，目标等待槽位保持 `pending` 状态不变，栈帧变量环境 $\rho$ 与步数计数器 `steps` 分毫无损，**不会触发节点的 `contract` 异常，也不会消耗节点的 `retry` 尝试次数**。

### 2. 阶段 B：原子认领与唤醒阶段（Atomic Claim & Wakeup Phase）

当请求通过阶段 A 验证后，更新处理器在工作流确定性状态机及重放过程中复核同一契约，并按以下两步无空窗交接：

1. **缓冲与返回外部闭合信封**：将经过白名单投影与无损归一化的外部响应 `(branch, output)` 写入槽位并作为 Update 确认返回值返回给调用方，同时将槽位标记为已投递（`delivered`）并从待认领注册表中移除。Update 处理器内部**不提前物化或返回内部 `rid`**，既防止向外部泄露私有运行期句柄标识，也避免在等待协程尚未恢复挂接可达根前被其他并发分支的根扫描误回收；
2. **唤醒后同步物化与原子提交**：等待在该槽位上的节点协程唤醒并通过工作流取消检查后，在无任何异步挂起点（`await`）的同一同步步骤内将外部句柄信封物化为内部能力信封（按 [`05-resource-lifecycle.md`](./05-resource-lifecycle.md) 登记或复用规范 `rid`）并挂接至待提交结果可达根，随后将 `(branch, output)` 作为本次尝试的 `ExecutionSuccess` 交付运行时调度器：
   * 普通顺序节点立即按右优先覆盖算子 $\rho \leftarrow \rho \triangleleft \Delta_{\text{valid}}$ 提交至所属栈帧并推进控制令牌；
   * Fork-Join 并发成员与 `for_each` 迭代成员将产出写入各自的并发暂存槽，待所在并发屏障内全部成员收敛后，按 [`02-fork-join-and-for-each-scheduler.md`](../04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md) 原子合并回父栈帧（`for_each` 迭代在挂起等待期间持续持有其 `concurrency` 并发信号量槽位）。

---

## 四、 编译期异常空间收紧与运行期超时、重试、取消语义

### 1. 可恢复异常论域收紧为 `{"timeout"}`

由于 `signal` 节点不执行外部业务代码，且外部非法信号在验证边界直接被拒绝（`422`）而不进入节点异常状态机，运行期 `signal` 节点**仅可能因等待超期产生 `timeout` 可恢复异常**，永不产生 `execution` 或 `contract` 异常：

* **允许选择器集合**：$\mathcal{S}(N_{\text{signal}}) = \{\texttt{"timeout"}\}$。在 `retry.on` 或 `catch.<branch>` 中显式声明 `"execution"`、`"contract"` 或 `"max_steps"` 均在编译期报 `schema.selectors` 错误；
* **`retry.on` 默认值**：当 `signal` 节点声明 `retry` 而省略 `on` 时，默认 `retry.on` 自动归一为 `["timeout"]`；
* **`catch` 通配符求补**：当 `signal` 节点的 `catch` 声明通配符 `"*"` 时，仅针对单元素全集 `{"timeout"}` 求补（若另一捕获分支已显式声明 `["timeout"]`，则 `"*"` 因展开为空集报 `schema.catch` 错误）。

### 2. 单次等待超时、原地重试与同轮次竞态裁决

* **无限期等待与 `timeout_ms` 预算**：
  * 未声明 `timeout_ms` 时，`signal` 节点无限期挂起等待，不受 Activity 默认超时上限约束；
  * 声明 `timeout_ms: T` 时，每次尝试享有独立的 $T$ 毫秒等待预算；每次进入 `retry` 重试时，复用节点激活前锁定的同一份只读输入快照、重新注册 `pending` 等待槽位并重置 $T$ 毫秒计时器。
* **步数计量不变性**：
  * 遵循 [`03-guardrails-and-step-accounting.md`](../04-runtime-execution-model/03-guardrails-and-step-accounting.md)，仅在节点激活（或 `for_each` 元素迭代启动）时递增 1 次所属图栈帧的 `steps`；挂起等待期间、边界拒绝非法信号以及节点内部的 `timeout` 原地重试均**不额外递增** `steps`。
* **同轮次认领与计时器到期竞态裁决**：
  * 若外部信号已在当前调度轮次内同步认领了等待槽位（`status == "delivered"`），即使单次尝试的 `timeout_ms` 定时器在同一轮次内同时到期，运行时**必须保留已认领的信号结果**，不得将该次已向调用方确认成功的投递改判为 `timeout`；
  * 若定时器到期时槽位仍处于 `pending` 状态，则立即将槽位标记为 `expired` 并从挂起注册表中移除（后续迟到的外部信号将收到 `409 signal_not_waiting`），随后抛出 `timeout` 异常进入节点的 `retry` / `catch` 状态机；
  * 若工作流整体收到外部取消（Cancel）或并发兄弟分支触发 Fail-Fast 协同取消，所有退出路径（含同轮次刚被认领但所属工作流已取消的情形）均强制向上传播 `CancelledError` 并清理挂起槽位，确保不残留任何悬挂槽位。

---

## 五、 跨子工作流（Child Workflow）的透明地址路由

当工作流在运行期将跨包调用节点（`use: workflow:<PackagePath>`）移交至后端子工作流执行实例时，宿主对外部调用方完全隐藏内部子工作流拆分层级，保证状态查询与 `:signal` 投递在统一的全局拓扑地址空间下运作：

1. **快照合并时的等待地址前缀重写**：
   * 设父工作流中委托给子工作流的调用帧逻辑地址为 $\text{prefix}$（如 `"__main__/sub_call"`），子工作流内部根帧的入口地址为 $\text{child\_entry}$（如 `"billing.approve"`）；
   * 宿主在递归拼接活跃栈帧树时，对子工作流内每一帧的 `address` 及 `waiting` 列表中的每个等待地址 $\text{addr}$，统一执行前缀替换：
     $$\text{addr}_{\text{external}} = \text{prefix} + \text{removeprefix}(\text{addr}, \text{child\_entry})$$
2. **信号投递时的路径段边界递归寻址**：
   * 宿主收到针对 `(execution_id, address)` 的信号投递请求后，首先固定根执行轮次（若根执行已处于终态，直接返回 `409 signal_not_waiting`）；
   * 从根工作流开始逐层读取运行期栈帧快照，将外部目标地址转换为当前层局部地址 $\text{local\_target}$，并按**严格路径段边界条件**查找是否存在持有子工作流链接的帧 $\text{remote\_frame}$：
     $$\text{local\_target} == \text{remote\_frame.address} \;\lor\; \text{starts\_with}(\text{local\_target}, \text{remote\_frame.address} + \texttt{"/"})$$
     *(注：严格以 `/` 作为路径段边界，防止 `"main/call"` 误匹配兄弟节点 `"main/call_other"` 或迭代帧 `"main/call[0]"`；其中相等条件 `==` 对应被调用的子工作流自身即为顶层 `RootRunNode(signal)` 的情形)*；
   * 若命中 $\text{remote\_frame}$，则固定该子工作流的执行轮次并递归向下解析，直至定位到直接持有目标等待槽位的工作流实例并完成原子信号投递，最终刷新并返回根 `Execution` 资源快照。
