# 05. 不透明资源句柄、授权与跨边界生命周期规范

本规范定义不透明资源句柄（`type: "handle"`）的传输信封与内部元数据表示、三层运行时组件（`WorkflowResourceRegistry`、`ResourceStore`、`AttemptResourceContext`）的权威职责划分、Activity 与独立 Child Workflow 的资源交接协议、基于可达根集合的确定性回收机制，以及 Sticky 亲和路由与失联收尾边界。业务侧资源类编写与 CLI 用法见[05. 资源句柄](../../docs/reference/05-resource-handles.md)。

---

## 一、 资源句柄身份、内部描述符与三层权威职责

### 1. 公开传输信封（Public Handle Envelope）

在工作流栈帧变量环境 $\rho$、内建算子求值与 Activity / Child Workflow 输入输出载荷中，非空资源句柄统一表示为仅包含两个非空字符串字段的闭合 JSON 对象：

```json
{
  "$handle": "res_0123456789abcdef0123456789abcdef",
  "kind": "infra.postgres:Tx"
}
```

* **信封闭合性**：仅允许出现 `"$handle"`（全局唯一资源标识 `rid`）与 `"kind"`（`<ModulePath>:<PublicClassName>`）两个键；
* **值与授权分离**：传输信封通过 `TypeSpec` Schema 校验仅证明其数据结构与名义类型匹配，**不自动赋予底层资源对象的访问或重建权限**；实际访问授权由运行时根据当前调用获准的输入句柄集合与本次尝试新分配集合独立核对；
* **句柄提取位置约束**：运行时仅根据节点或工作流的静态 `TypeSpec` 在声明为 `handle`（或递归包含 `handle` 的容器/结构体）的位置识别并提取句柄，绝不将普通 `string` / `object` 业务字段中碰巧包含 `"$handle"` 键的数据误判为受管资源句柄。

### 2. 内部资源描述符（`ResourceDescriptor`）

工作流控制面在内部账本中为每个受管资源维护规范化的 `ResourceDescriptor`：

| 字段 | 类型 | 契约说明 |
| :--- | :--- | :--- |
| **`rid`** | `NonEmptyString` | 全局唯一资源标识（如 `res_<32-hex>`），创建后不可变。 |
| **`kind`** | `HandleKind` | 逻辑资源类型标识 `<ModulePath>:<PublicClassName>`，创建后不可变。 |
| **`locator`** | `JSONValue \| null` | 资源实例的 JSON 可序列化重建定位符（经无损 JSON 编解码深拷贝隔离）；瞬态资源为 `null`，创建后不可变。 |
| **`persistent`** | `Boolean` | 是否代表独立于单个 Worker 进程存活的外部持久化实体（`@resource(persistent=True)`），创建后不可变。 |
| **`creator_queue`** | `NonEmptyString` | 首次创建该资源的 Worker 专属队列名（格式为 `<base_queue>-worker-<32-lowercase-hex>`），创建后不可变。 |
| **`resident_queues`** | `List<NonEmptyString>` | 已知持有该资源本地缓存副本的 Worker 专属队列有序集合（仅允许单调扩充并在回收后清除）。 |

### 3. 三层运行时组件权威职责划分

| 运行时组件 | 驻留位置 | 权威职责边界 |
| :--- | :--- | :--- |
| **`WorkflowResourceRegistry`** | Workflow 解释器实例内 | 维护当前工作流 Run 内 `owned`（拥有所有权与最终回收责任）与 `borrowed`（从父 Run 借入）的资源身份账本、在途调用的调度授权集合、已知 Worker 驻留队列集合，以及根集合扫描后的 `pending_dispose` 待回收账本。 |
| **`ResourceStore`** | Activity Worker 进程内 | 持有进程内真实 Python 资源对象实例、逐 `rid` 异步同步锁、当前活跃尝试的本地使用计数（`active_users`）以及本地 `pending_dispose` 标记，负责执行 `locator`、`restore` 与 `dispose` 生命周期钩子。 |
| **`AttemptResourceContext`** | 单次 Activity 尝试上下文内 | 维护本次尝试获准借用的输入资源集合（`allowed`）与本次尝试新注册的资源集合（`created`），向业务代码暴露 `await resources.put(instance)` 与 `await resources.get(handle)`，并在尝试失败、取消或输出裁剪时承担未移交新资源的本地孤儿清理责任。 |

### 4. 资源类协议与双向包映射核验

1. **装饰器与三类资源形态**：
   资源类通过 `@resource` 或 `@resource(persistent=True)` 装饰（仅直接装饰的类为合法资源类，未直接装饰的子类不被视为资源类；钩子函数均支持同步返回或异步 `awaitable`）：
   * **瞬态资源（Transient，`persistent=False` 且无定位协议或 `locator()` 返回 `None`）**：仅驻留于创建它的 Worker 进程内存，不可跨 Worker 重建；
   * **可恢复非持久资源（Restorable Non-Persistent，`persistent=False` 且成对提供 `locator` 与 `@classmethod/@staticmethod restore`）**：进程内缓存未命中或漂移至其他 Worker 时可通过 `restore(locator)` 重建等价访问客户端；回收时仅析构已驻留的进程内副本，不为析构而新建连接；
   * **可恢复持久资源（Persistent，`persistent=True`）**：必须同时定义返回非 `None` JSON 值的 `locator(self)`、`restore(cls, locator)` 与幂等的 `dispose(self)`；回收时若所有已缓存副本均不可达，可调度至目标池共享队列通过 `restore(locator)` 恢复定位包装并执行外部幂等删除。
2. **公开导出与双向模块映射核验**：
   * 资源 `kind` 为 `<ModulePath>:<PublicClassName>` 时，Worker 通过固定根映射表 `module_mounts` 将 `<ModulePath>` 解析为物理 Python 资源包 `<py_pkg>`，并从 `<py_pkg>.__init__.py` 导入同名公开属性 `<PublicClassName>`；
   * 该类对象的定义模块 `cls.__module__` 的所属包 `sys.modules[cls.__module__].__package__` 必须严格等于 `<py_pkg>`（允许直接定义在 `<py_pkg>/__init__.py` 或同目录单文件子模块如 `<py_pkg>/_tx.py` 中并在 `__init__.py` 公开导出，严禁跨包重导出其他包的资源类）；
   * 调用 `resources.put(instance)` 时，`ResourceStore` 从 `type(instance)` 的定义包经 `module_mounts` 逆映射推导逻辑 `<ModulePath>:<PublicClassName>`，并正向复核该包公开导出属性确为同一类对象（运行期 `ResourceStore` 仅依赖已安装的 Python 包与类协议，不读取磁盘上的 `__ax_resources__.yaml`）。

---

## 二、 Activity 与独立 Child Workflow 的资源交接协议

### 1. Activity 内部外壳与所有权移交

业务执行器（`builtin`、`module` 及自定义扩展执行器）始终返回标准业务响应 `{ branch, output }`。外层 Activity 运行时在完成业务求值后，将响应统一封装为内部交接外壳返回给工作流：

```json
{
  "result": {
    "branch": "optional_chosen_branch_id_or_null",
    "output": { "...": "..." }
  },
  "resources": {
    "created": [ /* 本次尝试新创建且出现在最终有效输出中的 ResourceDescriptor 列表 */ ],
    "worker_queue": "<base_queue>-worker-<32-lowercase-hex>",
    "resident_rids": [ /* 本次获准输入及新导出资源中当前仍驻留于本 Worker 的 rid 列表 */ ],
    "default_queue": "<workflow_default_task_queue>"
  }
}
```

*(注：`default_queue` 由工作流请求载荷传入并随 Activity 结果记录在事件历史中，确保离线重放 `Replayer` 使用重放器随机队列时仍按历史记录的原始默认队列执行路由校验。)*

* **输入授权边界**：Activity 启动时，按节点 `input_schema` 从 `input_snapshot` 提取的句柄集合必须与工作流下发的授权描述符列表 `resource_context.resources` 完全一致；业务代码调用 `resources.get(handle)` 时仅允许访问本次获准输入句柄或本次尝试通过 `resources.put(...)` 新创建的句柄（手工伪造信封或自携 `locator` 均无法通过授权核验）。
* **先校验、后移交（Validate-Before-Handoff）与孤儿清理**：
  * 仅当分支合法性校验、活跃分支 `output` 白名单投影、深层 `TypeSpec` Schema 校验以及输出句柄来源核验（输出中出现的每个 `rid` 必须属于获准输入或本次 `created`）**全部通过**后，Activity 才将实际出现在最终有效输出中的新分配资源列入 `resources.created` 移交工作流；
  * 本次尝试中分配但未出现在最终有效输出中的局部资源，或当尝试因业务异常、契约违约、取消而失败时的**全部**新分配资源，均留在当前 Attempt 本地作为孤儿资源立即执行 `dispose` 清理，绝不向工作流移交半成品所有权。
* **本地期限裁决与 Server 硬超时边界**：
  * Activity 在业务协程返回且完成全部输出契约校验的边界检查本地单调时钟 `deadline`（预算 $A$）并锁定业务结果；随后的本地孤儿资源清理使用独立的有界清理预算，不因孤儿清理耗时将已按时完成的业务成功改判为本地 `timeout`（若尝试在完成前已被真实取消，则仍撤销全部新分配资源）；
  * 后端的 `start_to_close_timeout` 保持为预算 $A$；若本地清理或网络传输期间触及后端硬超时，工作流仍按后端 `timeout` 处理并触发取消收尾。
* **失败与取消时的驻留上报**：
  * 当 Activity 抛出业务或契约异常时，`ApplicationError` 的附加详情在保留异常选择器的同时携带已发生的驻留队列更新与业务任务排空状态（`drained`）；
  * 当 Activity 因超时或取消无法通过正常返回值交接时，Worker 在本地业务任务实际收尾后可向发起该调用的确切 Workflow Run 发送一次绑定调用 ID（`call_id`）的 `resource_update` 信号，上报本次尝试期间已实际创建的孤儿清理状态或驻留记录。

### 2. 独立 Child Workflow 的借入与所有权移交

* **进程内子图 vs 独立 Child Workflow**：
  * 同一工作流实例内的内联 `graph`（`BlockFrame`）与进程内 `use: workflow:<PackagePath>`（`FunctionFrame`）直接共享同一个 `WorkflowResourceRegistry`；
  * 当 `use: workflow:<PackagePath>` 切换为后端独立 Child Workflow 实例时，父子实例各自维护独立的 `WorkflowResourceRegistry`，通过与 Activity 同构的 `{ result, resources }` 外壳完成跨实例交接。
* **借入（Borrowed）与新分配（Owned）分离**：
  * 父工作流传入 Child Workflow 的输入句柄在子实例中注册为 **`borrowed`**（子实例可使用并更新其驻留队列，但在子实例内局部不可达或子实例失败退出时，**严禁**由子实例销毁父工作流借出的资源）；
  * 子实例内部新创建的资源在子实例运行期间为 `owned`；当子实例正常完成时，其完整出口输出中引用的新创建资源随 `resources.created` 完整移交父工作流（转为父工作流 `owned`），父工作流先完成所有权接管，再按调用节点的 `with.output` 投影映射并在父栈帧中通过根扫描回收未被父节点接收的多余输出资源；
  * 子实例退出前仅清理子实例自身创建且未随正常返回值移交父工作流的剩余 `owned` 资源。

---

## 三、 可达根集合扫描与确定性回收（Reachability-Based Disposal）

### 1. 三类可达根集合（Reachability Roots）

`WorkflowResourceRegistry` 不采用易受环路或重放干扰的逐变量引用计数，而是在每次状态变更的同一同步步骤内更新根集合并重算可达闭包 $\mathcal{R}_{\text{live}}$。可达根集合严格由以下三部分并集构成：

1. **活跃执行栈帧环境根**：当前所有活跃 `FunctionFrame` 与 `BlockFrame` 的变量环境 $\rho$ 中按各自静态符号表 $\Sigma_G$ 提取的句柄 ID（`IterationFrame` 仅作为只读诊断视图，不独立构成资源根）；
2. **待提交完整结果根（Pending Results）**：子栈帧退出前尚未完成父节点 `with.output` 映射的完整输出，以及 Fork-Join 并发组或 `for_each` 同步屏障内已完成分支暂存的返回结果；
3. **在途调用输入根（In-Flight Call Inputs）**：从 Activity 或独立 Child Workflow 派发开始、直至该次调用正常完成交接或调度授权结束为止的锁定输入句柄集合。

*(注：对外状态观察快照、执行轨迹 `trace`、未确认排空的失联调用记录以及 `pending_dispose` 队列本身均不属于可达根。)*

### 2. 触发时机与状态转移

* **同步触发点**：节点输出原子提交、分支反定值抹除（`Unbound`）、子栈帧退出替换、并发组异常排空完成以及在途调用收尾时，同步触发根集合扫描；
* **转入待回收（`pending_dispose`）**：扫描后，凡满足 `ownership == "owned"` 且 $\text{rid} \notin \mathcal{R}_{\text{live}}$ 的资源立即从活跃表移入 `pending_dispose`（若 `ownership == "borrowed"` 且局部不可达，则仅从子工作流注册表中释放借用记录，不触发销毁）；
* **调度清理命令**：在同步根扫描结束后，工作流针对 `pending_dispose` 中的资源向对应的 Worker 专属队列（或持久资源的共享池队列）派发 `dispose_resources` 清理 Activity；工作流结束退出前，对所有剩余 `owned` 资源执行最终兜底回收。

---

## 四、 `ResourceStore` 进程内同步与三态回收结果

1. **逐 `rid` 异步同步边界**：
   * `ResourceStore` 针对每个 `rid` 维护独立的异步锁，串行化该资源的 `restore` 恢复、尝试使用登记（`acquire`）与 `dispose` 析构检查，彻底消除“边析构边恢复”或“清理后重新入表”的竞态；
   * 业务代码在 `execute()` 中调用资源实例的业务方法在锁外执行；但该次尝试的 `active_users` 使用登记一直保留至业务协程及其派生子任务**实际排空退出**为止。
2. **`pending_dispose` 屏障与延迟析构**：
   * 一旦某 `rid` 被标记为 `pending_dispose`，后续任何新的 `resources.get(handle)` 请求立即被拒绝；
   * 若此时仍有未排空的本地尝试正在使用该实例（`active_users > 0`），`ResourceStore` 等待最后一个本地使用者排空退出后，再自动触发 `dispose()`。
3. **`dispose_resources` 的三态返回契约**：
   清理 Activity 对每个目标 `rid` 返回以下三种状态之一及诊断信息：
   * **`disposed`**：本地实例已成功执行 `dispose()` 并从 `ResourceStore` 摘除（不保留永久墓碑）；
   * **`missing`**：当前 Worker 的 `ResourceStore` 中不存在该 `rid` 的实例（仅说明本进程无缓存副本，既不覆盖其他队列此前的析构失败，也不单独构成持久化外部实体已删除的证明）；
   * **`unconfirmed`**：该 `rid` 仍有未排空的本地使用者，或执行 `restore()` / `dispose()` 期间抛出异常（此时保留原实例与 `pending_dispose` 标记，供后续收尾或 Worker 停机时再次尽力清理；析构异常仅记录诊断，不覆盖已确定的业务结果或业务异常）。

---

## 五、 亲和路由（Sticky Routing）、失联收尾预算与复合 Worker

1. **队列路由优先级與 Sticky 回退**：
   * Activity 的目标 Worker 池基队列 `<base_queue>` 由 `options.task_queues[executor]` 或工作流默认队列决定；
   * **含瞬态资源输入时**：所有瞬态输入资源必须驻留在目标池的同一个 Worker 专属队列 `<base_queue>-worker-<id>` 上，并直接路由至该专属队列；若分属不同 Worker 或队列不兼容，立即产生 `execution` 错误；
   * **全部输入资源均可恢复时**：优先路由至目标池最近一次成功执行的 Sticky Worker 专属队列，其次选择输入资源描述符中首个兼容的已驻留专属队列，最后回退至共享池队列 `<base_queue>`；
   * **Sticky `schedule_to_start` 5 秒回退**：投递至 Worker 专属队列的调用设置 $5\text{ s}$ 的 `schedule_to_start_timeout`；若专属 Worker 在启动前失联超时且全部输入资源均可恢复，工作流自动回退至共享队列 `<base_queue>` 重新调度（若调用已启动后发生超时或取消，则必须先执行取消与有界收尾，严禁将其视为未领取任务直接透明重投）。
2. **30 秒共享收尾预算与失联边界**：
   * 当含资源句柄的调用发生超时或取消时，工作流侧的取消排空等待与本轮已知资源清理共享 **$30\text{ s}$ 总收尾预算**（各步骤消耗剩余预算，重复取消不续期）；
   * 收尾预算耗尽后，工作流结束旧调用的调度授权并继续推进 `retry` / `catch`；远端 Worker 继续保活尚未实际退出的本地使用者并在其排空后清理本地孤儿资源；
   * 框架不承诺进程崩溃窗口内的绝对零外部泄漏或超时后的 `exactly-once` 副作用——持久化外部资源应结合精确版本号/代次标识实现幂等 `dispose()`，并为底层外部存储配置独立的 TTL 兜底策略。
3. **复合 Worker（Composite Worker）队列拓扑与优雅停机**：
   * `create_worker` 为每个 Worker 进程分配唯一的 `<base_queue>-worker-<32-lowercase-hex>` 专属队列，并在同一进程内组合监听共享队列与专属队列，共享同一个 `ResourceStore` 与 `module_mounts`；
   * 共享队列注册工作流解释器与全部 Activity（`execute_run`、`dispose_resources`），专属队列仅注册 Activity（`execute_run`、`dispose_resources`）；
   * Worker 正常停机时，先停止接收新任务并排空在途 Activity，随后关闭 `ResourceStore`：对进程内所有非持久资源缓存执行 `dispose()`，而对仍归活跃工作流 Run 所有的持久资源（`persistent=True` 且未处于 `pending_dispose`）仅摘除本地内存引用，避免误删仍在运行工作流的外部持久化实体。
