# 05. 不透明资源句柄、授权与跨边界生命周期规范

本规范定义不透明资源句柄（`type: "handle"`）的三类闭合信封表示与内部元数据契约、三层运行时组件（`WorkflowResourceRegistry`、`ResourceStore`、`AttemptResourceContext`）的权威职责划分、正交所有权状态机与公开定位符规范身份、Activity 与独立 Child Workflow 的资源交接协议、基于可达根集合的确定性回收机制，以及宿主根边界移交、Sticky 亲和路由与写入时观测投影规则。业务侧资源类编写与 CLI 用法见 [05. 资源句柄](../../docs/reference/05-resource-handles.md)。

---

## 一、 资源句柄信封、描述符契约与三层权威职责

### 1. 三类闭合句柄信封表示（Closed Handle Envelopes）

运行时与宿主接口仅根据静态 `TypeSpec` 在声明为 `handle`（或递归包含 `handle` 的容器/结构体）的位置识别并处理句柄信封；普通 `string` / `object` 业务字段即使碰巧包含 `"$handle"` 键也原样透传。非 `null` 句柄按内部执行与外部边界划分为键集合严格互斥的三类闭合 JSON 对象：

| 作用域与边界 | JSON 闭合结构 | 键集合与契约说明 |
| :--- | :--- | :--- |
| **1. 内部运行期能力信封**<br>*(Workflow `env` / `builtin:*` / Activity / Child IPC)* | `{"$handle": "res_018f...", "kind": "infra.postgres:Tx"}` | 闭合双键 `{"$handle", "kind"}`。`"$handle"` 为运行期资源标识 `rid`，仅限执行内部流转，绝不接受外部输入，也绝不跨越外部查询或结果边界暴露。 |
| **2. 外部可寻址定位符信封**<br>*(根 `input` / 根 `output` / `signal` `output` / `frames` / `trace`)* | `{"$handle": "infra.storage:File", "locator": {"file_id": "f_1"}}` | 闭合双键 `{"$handle", "locator"}`。仅适用于在 `bundle.resources` 中声明了公开 `locator` 的资源；`"$handle"` 精确等于槽位 `kind`，`"locator"` 满足对应非可空纯值 `TypeSpec`。 |
| **3. 不可寻址句柄诊断视图**<br>*(仅只读出现在 `frames` / `trace` 中)* | `{"$handle": "infra.postgres:Tx"}` | 闭合单键 `{"$handle"}`。用于未声明公开 `locator` 的瞬态资源或内部私有可恢复资源；既与 `null` 区分，又严格隐藏内部 `rid` 与敏感恢复定位符，不可提交至任何外部输入边界。 |

* **值与授权分离**：无论是内部能力信封还是外部定位符信封，通过 `TypeSpec` 校验仅证明数据结构与名义类型匹配，**不自动赋予底层资源对象的访问权限**——执行内部由 `WorkflowResourceRegistry` 与 `AttemptResourceContext` 按调用获准输入与新分配集合独立核对授权；宿主外部边界由控制面入口预检或业务资源的签名定位符协议完成归属鉴权。

### 2. 内部资源描述符（`ResourceDescriptor`）、正交所有权与规范身份

#### (a) `ResourceDescriptor` 字段与结构不变量

工作流控制面在内部账本中为每个受管资源维护规范化的 `ResourceDescriptor`：

| 字段 | 类型 | 契约说明 |
| :--- | :--- | :--- |
| **`rid`** | `NonEmptyString` | 运行期唯一资源标识（Worker 新建为 `res_<32-hex>`，外部边界物化按 `(run_id, seq)` 确定性生成），创建后不可变。 |
| **`kind`** | `HandleKind` | 逻辑资源类型标识 `<ModulePath>:<PublicClassName>`，创建后不可变。 |
| **`locator`** | `JSONValue \| null` | 资源实例的 JSON 可序列化重建定位符（经无损 JSON 编解码深拷贝隔离）；瞬态资源为 `null`，创建后不可变。 |
| **`persistent`** | `Boolean \| null` | Worker 通过 `resources.put()` 创建时记录类协议的精确布尔值（根出口升格 `external=true` 后仍保持原布尔值不变）；从外部边界物化时在整个生命周期内恒为 `null`（Worker 恢复时不向 Workflow 回写该字段，本地直接依据 `@resource` 类协议判定持久性）。 |
| **`creator_queue`** | `NonEmptyString \| null` | Worker 创建时为其专属队列名；从外部边界物化时恒为 `null`。 |
| **`external`** | `Boolean` | 外部持久实体（若存在）的所有权是否归属外部宿主。外部边界供应时为 `true`，内部新建时为 `false`；根工作流正常成功完成时可将根 `output` 导出的条目单向升格为 `true`。 |
| **`resident_queues`** | `List<NonEmptyString>` | 已知持有该资源进程内缓存副本的 Worker 专属队列有序集合（单调扩充，回收确认后清除）。 |

* **结构良构不变量**：
  * `(persistent is null) == (creator_queue is null)`；
  * 当 `persistent is null` 时，必须满足 `external == true` 且 `locator != null`；
  * 当 `persistent` 为布尔值且 `persistent || external` 为真时，必须满足 `locator != null`；
  * **不可变身份比较**：描述符身份一致性核验排除可变状态字段 `resident_queues` 与单向演化的 `external`，其余字段自创建起严禁改写。

#### (b) 正交双维度所有权模型（`owned` $\times$ `external`）

运行时将**当前执行实例在调用树中的驻留回收管辖权（账本条目属性 `owned: Boolean`）**与**底层持久实体的宿主归属保护（描述符属性 `external: Boolean`）**正交分离（其中 `dispose_resources` 清理指令的 `deleted` 参数表示“外部持久实体已无需删除或禁止删除”）：

| `owned` | `external` | 产生与流转场景 | 当前 Run 不可达或退出时的回收责任 |
| :---: | :---: | :--- | :--- |
| `true` | `false` | 当前 Run（或其已合并的子调用）通过 `resources.put()` 新建、且尚未移交宿主或父 Run 的内部资源 | 向 `resident_queues` 派发 `dispose_resources`；若 `persistent == true` 且外部实体尚未确认删除，可回退至 `creator_queue` 所属池共享队列恢复定位包装并执行幂等外部删除。 |
| `true` | `true` | ① 当前 Run 从外部定位符物化的资源；<br>② 子 Run 物化外部资源后随正常返回值上交父 Run；<br>③ 根 Run 正常成功完成时将根 `output` 导出的内部资源单向升格为 `external=true` | 向 `resident_queues` 派发携带 `deleted=true` 的 `dispose_resources`（关闭非持久类的本地连接，摘除持久类的内存包装缓存，永不删除底层持久实体），跳过 `creator_queue` 回退恢复。 |
| `false` | `false` | 父 Run 将内部新建资源传入独立 Child Workflow 借用（`borrowed`） | **不派发 `dispose_resources`**（防止提前析构父 Run 仍在使用的瞬态连接或缓存），仅向父 Run 汇报累计驻留队列。 |
| `false` | `true` | 父 Run 将外部供应资源传入独立 Child Workflow 借用（`borrowed`） | **不派发 `dispose_resources`**，仅向父 Run 汇报累计驻留队列，由父 Run 统一驱逐缓存。 |

#### (c) 公开 `locator` 的规范身份与代理键归并（Canonical `(kind, locator)` Identity）

对于在 `bundle.resources` 中声明了公开 `locator` 的资源类型，**`(kind, locator)`（按无损 JSON 字面量相等性比较）即该资源在单次顶层执行内的规范身份**，`rid` 作为活跃账本中与 `(kind, locator)` 一一对应的内部代理键；未声明公开 `locator` 的资源保持每次 `resources.put()` 独立的 `rid` 身份：

1. **外部边界物化（根 `input` 与 Signal 恢复）**：在当前 Run 尚未进入 `pending_dispose` 的活跃账本中查找同 `(kind, locator)` 的规范条目 `rid_canon`：
   * 若命中，直接复用 `rid_canon` 并**保持其原有 `external` 标记不变**（内部新建草稿句柄经观测后由 `:signal` 重新选回时仍维持 `external=false`，不提前免除工作流失败时的清理义务）；
   * 若未命中，按 `(run_id, seq)` 确定性分配新 `rid`，登记 `owned=true, external=true, persistent=null, creator_queue=null` 的描述符。
2. **跨 Activity / Child `created` 交接归并**：接收子调用返回的 `resources.created` 时，若某个公开可寻址新条目命中活跃账本（或同批次前序条目）已有的同 `(kind, locator)` 规范条目 `rid_canon`，则将返回 `output` 中出现的重复 `rid` 统一重写为 `rid_canon`、保留 `rid_canon` 原有的 `external` 不变，并将重复 `rid` 已产生的 `resident_queues` 以 `owned=true, external=true, pending_dispose=true` 排入待清理队列，仅驱逐多余 Worker 包装副本而不触碰共享底层持久实体。

### 3. 三层运行时组件权威职责划分

| 运行时组件 | 驻留位置 | 权威职责边界 |
| :--- | :--- | :--- |
| **`WorkflowResourceRegistry`** | Workflow 解释器实例内 | 维护当前工作流 Run 内 `owned` 与 `borrowed`（`owned=false`）的资源账本、公开 `(kind, locator)` 规范身份映射、在途调用的调度授权集合、已知 Worker 驻留队列集合，以及根集合扫描后的 `pending_dispose` 待回收队列。 |
| **`ResourceStore`** | Activity Worker 进程内 | 持有进程内真实 Python 资源对象实例、逐 `rid` 异步同步锁、单向外部删除权撤销记录、当前活跃尝试的本地使用计数（`active_users`）以及本地 `pending_dispose` 标记，负责执行 `locator`、`restore` 与 `dispose` 生命周期钩子。 |
| **`AttemptResourceContext`** | 单次 Activity 尝试上下文内 | 维护本次尝试获准借用的输入资源集合（`allowed`）与本次尝试新注册的资源集合（`created`），向业务代码暴露 `await resources.put(instance)` 与 `await resources.get(handle)`，并在尝试失败、取消或输出裁剪时承担未移交新资源的本地孤儿清理责任。 |

### 4. 资源类协议与双向包映射核验

1. **装饰器与三类资源形态**：
   资源类通过 `@resource` 或 `@resource(persistent=True)` 装饰（仅直接装饰的类为合法资源类，未直接装饰的子类不被视为资源类；钩子函数均支持同步返回或异步 `awaitable`）：
   * **瞬态资源（Transient，`persistent=False` 且无定位协议或 `locator()` 返回 `None`）**：仅驻留于创建它的 Worker 进程内存，不可跨 Worker 重建；
   * **可恢复非持久资源（Restorable Non-Persistent，`persistent=False` 且成对提供 `locator` 与 `@classmethod/@staticmethod restore`）**：进程内缓存未命中或漂移至其他 Worker 时可通过 `restore(locator)` 重建等价访问客户端；回收时仅析构已驻留的进程内副本，不为析构而新建连接；
   * **可恢复持久资源（Persistent，`persistent=True`）**：必须同时定义返回非 `None` JSON 值的 `locator(self)`、`restore(cls, locator)` 与幂等的 `dispose(self)`；当归工作流所有且所有已缓存副本均不可达时，可调度至目标池共享队列通过 `restore(locator)` 恢复定位包装并执行外部幂等删除。
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
* **按需下发与双端零信任 `locator` Schema 校验**：
  * 当且仅当节点 `output` 类型递归包含 `handle` 时，工作流在 `resource_context.locator_schemas` 中按该 `output` 实际涉及的公开可寻址 `HandleKind` 子集附带对应的 `locator` `TypeSpec`（纯值 Activity 不携带该字段）；
  * Worker 在导出新创建的公开可寻址资源前校验其 `descriptor.locator` 非 `null` 且满足对应 `locator` `TypeSpec`；Workflow 在接收 Activity 或 Child Workflow 返回的 `resources.created` 时使用自身 `bundle.resources` 独立二次复核同一契约。任一侧违约均映射为节点可恢复的 **`contract`** 异常。
* **先校验、后移交（Validate-Before-Handoff）与孤儿别名安全清理**：
  * 仅当分支合法性校验、活跃分支 `output` 白名单投影、深层 `TypeSpec` Schema 校验、输出句柄来源核验以及公开 `locator` Schema 校验**全部通过**后，Activity 才将实际出现在最终有效输出中的新分配资源列入 `resources.created` 移交工作流；
  * 本次尝试中分配但未出现在最终有效输出中的局部资源，或当尝试因业务异常、契约违约、取消而失败时的**全部**新分配资源，均留在当前 Attempt 本地作为孤儿资源清理，绝不向工作流移交半成品所有权；
  * 在挂起等待任何异步析构钩子之前，`ResourceStore` 先在同一同步步骤内将整批本地孤儿标记为 `pending_dispose`，并对携带定位符的持久孤儿按 `(kind, locator)` 核对本次非孤儿授权输入、`ResourceStore` 中其他未待回收的存活/已接管条目及已撤销删除权的宿主条目：若命中存活或宿主别名，该孤儿仅在当次清理中让出外部删除权（不污染无关条目的宿主保护状态）；若同一批次或并发未导出的持久孤儿指向同一新建实体且无存活别名，则精确保留唯一一个实体删除责任者，其余副本仅摘除内存缓存；非持久资源包装始终各自关闭其本地连接。
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
  * 父工作流传入 Child Workflow 的输入句柄在子实例中注册为 **`borrowed`**（`owned=false`：子实例可使用并更新其驻留队列，但在子实例内局部不可达或子实例失败退出时，**严禁**由子实例销毁父工作流借出的资源）；
  * 子实例内部新创建或从 `:signal` 新物化的资源在子实例运行期间为 `owned=true`；当子实例正常完成时，其完整出口输出中引用的 `owned` 资源随 `resources.created` 移交父工作流（父工作流按第一节第 2(c) 小节完成规范身份归并与接管，随后按调用节点的 `with.output` 投影映射并通过根扫描回收未被父节点接收的输出资源）；
  * 子实例退出前仅按各自的 `external` 属性清理子实例自身剩余且未移交父工作流的 `owned` 资源。

---

## 三、 可达根集合扫描与确定性回收（Reachability-Based Disposal）

### 1. 三类可达根集合（Reachability Roots）

`WorkflowResourceRegistry` 不采用易受环路或重放干扰的逐变量引用计数，而是在每次状态变更的同一同步步骤内更新根集合并重算可达闭包 $\mathcal{R}_{\text{live}}$。可达根集合严格由以下三部分并集构成：

1. **活跃执行栈帧环境根**：当前所有活跃 `FunctionFrame` 与 `BlockFrame` 的变量环境 $\rho$ 中按各自静态符号表 $\Sigma_G$ 提取的句柄 ID（`IterationFrame` 仅作为只读诊断视图，不独立构成资源根）；
2. **待提交完整结果根（Pending Results）**：子栈帧退出前尚未完成父节点 `with.output` 映射的完整输出、Signal 唤醒后同步物化的待提交输出，以及 Fork-Join 并发组或 `for_each` 同步屏障内已完成分支暂存的返回结果；
3. **在途调用输入根（In-Flight Call Inputs）**：从 Activity 或独立 Child Workflow 派发开始、直至该次调用正常完成交接或调度授权结束为止的锁定输入句柄集合。

*(注：对外状态观察快照、执行轨迹 `trace`、未确认排空的失联调用记录以及 `pending_dispose` 队列本身均不属于可达根。)*

### 2. 触发时机与状态转移

* **同步触发点**：节点输出原子提交、分支反定值抹除（`Unbound`）、子栈帧退出替换、并发组异常排空完成以及在途调用收尾时，同步触发根集合扫描；
* **转入待回收（`pending_dispose`）**：扫描后，凡满足 `owned == true` 且 $\text{rid} \notin \mathcal{R}_{\text{live}}$ 的资源立即转入 `pending_dispose`；`owned == false` 的借入条目不触发销毁，其驻留记录在子工作流返回或收尾时汇总回父 Run；
* **调度清理命令**：在同步根扫描结束后，工作流针对 `pending_dispose` 中的资源向已知 `resident_queues` 派发 `dispose_resources` 清理 Activity：
  * 当 `descriptor.external == true` 时，固定携带 `deleted=true`，仅驱逐持久资源的本地内存包装缓存或关闭非持久资源的本地连接，以全部已知驻留队列确认清理为摘除条件；
  * 当 `descriptor.external == false` 时，携带当前已知的外部实体删除状态；若 `persistent == true` 且所有驻留副本清理后仍未确认外部实体删除，则回退至 `creator_queue` 所属池共享队列恢复定位包装并执行幂等外部删除。工作流结束退出前对所有剩余 `owned == true` 资源执行最终兜底回收。

---

## 四、 `ResourceStore` 进程内同步与三态回收结果

1. **逐 `rid` 异步同步边界**：
   * `ResourceStore` 针对每个 `rid` 维护独立的异步锁，串行化该资源的 `restore` 恢复、尝试使用登记（`acquire`）与 `dispose` 析构检查，彻底消除“边析构边恢复”或“清理后重新入表”的竞态；
   * 业务代码在 `execute()` 中调用资源实例的业务方法在锁外执行；但该次尝试的 `active_users` 使用登记一直保留至业务协程及其派生子任务**实际排空退出**为止。
2. **`pending_dispose` 屏障、类协议核验与删除权单向撤销**：
   * 一旦某 `rid` 被标记为 `pending_dispose`，后续任何新的 `resources.get(handle)` 请求立即被拒绝；若此时仍有未排空的本地尝试正在使用该实例（`active_users > 0`），`ResourceStore` 等待最后一个本地使用者排空退出后再自动触发清理；
   * Worker 统一依据目标 Python 类的实际 `@resource(persistent=...)` 装饰器协议判定资源持久性：若传入描述符的 `persistent` 非 `null` 则强制断言与类协议一致；若 `persistent` 为 `null`（外部边界物化描述符）则要求类定义了可调用的 `locator` 与 `restore`，并在首次 `restore(locator)` 后通过无损字面量判等复核 `instance.locator()` 与描述符定位符精确一致；
   * **宿主删除保护单向锁死**：一旦 `ResourceStore` 收到某 `rid` 的 `descriptor.external == true` 或 `dispose_resources(..., deleted=true)`，即在该 Store 生命周期内永久撤销该 `rid` 的外部持久实体删除权限（后续迟到的旧描述符或重复清理请求均不得恢复删除权）——清理时持久资源仅摘除内存缓存引用，非持久资源仍调用本地 `dispose()` 关闭连接。
3. **`dispose_resources` 的三态返回契约**：
   清理 Activity 对每个目标 `rid` 返回以下三种状态之一及诊断信息：
   * **`disposed`**：本地实例已按协议清理并从 `ResourceStore` 摘除（已撤销的外部删除权限记录继续保留）；
   * **`missing`**：当前 Worker 的 `ResourceStore` 中不存在该 `rid` 的实例（仅说明本进程无缓存副本，既不覆盖其他队列此前的析构失败，也不单独构成持久化外部实体已删除的证明）；
   * **`unconfirmed`**：该 `rid` 仍有未排空的本地使用者，或执行 `restore()` / `dispose()` 期间抛出异常（此时保留原实例与 `pending_dispose` 标记，供后续收尾或 Worker 停机时再次尽力清理；析构异常仅记录诊断，不覆盖已确定的业务结果或业务异常）。

---

## 五、 亲和路由（Sticky Routing）、失联收尾预算与复合 Worker

1. **队列路由优先级与 Sticky 回退**：
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
   * Worker 正常停机时，先停止接收新任务并排空在途 Activity，随后关闭 `ResourceStore`（依据目标 `@resource` 类的实际持久性协议而非可能为 `null` 的描述符字段处理）：对所有非持久缓存执行本地 `dispose()`；对未处于 `pending_dispose` 的持久缓存仅摘除本地内存引用；对已处于 `pending_dispose` 的持久缓存按其已锁定的外部删除权限决定是否执行外部幂等删除。

---

## 六、 宿主根边界、Signal 物化交接与写入时观测投影

1. **宿主根入口与独立 Child Workflow 入口分流**：
   * 工作流解释器严格按是否存在真实父调用上下文（`parent`）区分边界类型（逻辑调用深度 `call_depth` 不改变边界判定）：
     * **宿主根入口（`parent == null`）**：执行 `validate_root_interface(input, output, resources)` 与 `validate_external_input(input_json, input, resources)`，通过后同步将外部定位符信封物化进 `WorkflowResourceRegistry` 并构造根 `FunctionFrame`；
     * **独立 Child Workflow 入口（`parent != null`）**：执行内部能力信封校验（$\models_{\text{rt}}$），并将父工作流下发的输入资源登记为 `borrowed`（`owned=false`）。
2. **根出口去物化与无竞态所有权移交顺序**：
   * 当宿主根工作流（`parent == null`）主体正常返回时，先扫描根出口实际导出的 `exported` 句柄集合，并生成**独立的外部去物化结果副本**经 `validate_external_output` 复核（不原地改写内部结果结构，亦不提前释放结果可达根，确保收尾排空期间到达的 `resource_update` 信号仍能保护 `exported`；若外部输出复核失败则清空 `exported` 并转入 `contract` 失败路径）；
   * 随后的 `finally` 有界收尾阶段共享单一 $30\text{ s}$ 期限预算，依次执行以下三步：
     1. **排空内部处理器**：若已处于失败或取消态则清空 `exported`，等待所有在途 Update / Signal 处理器完成并取消排空冷启动定时器；
     2. **清理未导出内部资源**：以 `exported` 作为保护集回收所有其余内部资源；
     3. **最终终态裁决与宿主移交**：释放内部结果可达根并复查失败与取消状态——若工作流失败或被取消（含第二步期间发生的取消），保留原 `external=false` 对此前受保护的内部新建资源执行有界尽力删除后抛出对应异常；若确认无失败且未取消，**正式锁定成功完成态**（后续不再响应取消改判），将 `exported` 中所有条目的 `descriptor.external` 及历史在途调用记录中对应的授权描述符副本同步单向置为 `true`（防止迟到的 `resource_update` 用旧描述符复活外部删除），再以 `deleted=true` 驱逐各驻留 Worker 的内存缓存并返回外部闭合结果载荷。
3. **运行期快照与执行轨迹的写入时即时投影**：
   * 为确保历史轨迹在对应 `rid` 被回收摘除后仍完整保留公开定位符且绝不泄露内部 `rid` 或私有恢复参数，`get_runtime_snapshot` 与全部轨迹记录点必须在**构造快照或写入轨迹载荷当时**按静态 `TypeSpec` 即时完成句柄投影（公开 `locator` 资源投影为 `{"$handle": "<kind>", "locator": <value>}`，其余资源投影为 `{"$handle": "<kind>"}`）：
     * 活跃栈帧快照：每个 `Frame` 按其静态符号表投影 `variables`；
     * 根工作流 `workflow` Span：直接记录入口校验后的外部输入与出口去物化后的外部结果；独立 Child Workflow 的 `workflow` Span 按被调根工作流的 `input` / `output` 投影；
     * `builtin:*` 节点：输入按 `bindings.input` 的源路径静态类型推导投影，输出按目标输出类型投影；
     * `use: workflow:<PackagePath>` 节点：输入按被调方 `root.input` 投影，输出按调用方目标变量类型投影；
     * `for_each` 外层聚合 Span 按父帧集合类型投影；其余 `run` 与内联 `graph` 节点按各自的输入符号表与输出契约投影。
