# RFC-0019: 外部可寻址资源定位符（Public Locator）、闭合外部句柄信封与跨宿主边界生命周期规范

* **状态**：Draft
* **所属分类**：资源声明扩展、外部句柄传输契约与宿主边界所有权模型（Public Resource Locators, External Handle Envelopes & Host-Boundary Ownership）
* **关联提案**：
  * [`RFC-0011: Axcutor 编译器前后端与 Temporal 运行时参考实现架构`](./0011-compiler-and-temporal-runtime-architecture.md)
  * [`RFC-0013: 不透明句柄类型与跨 Activity / Workflow 资源生命周期`](./0013-opaque-handle-type-and-resource-registry.md)
  * [`RFC-0017: 协作式挂起执行器 signal、Temporal Update 恢复协议与宿主 :signal 接口规范`](./0017-cooperative-signal-executor.md)
  * [`RFC-0018: 按根挂载编译单元（MountUnit）、__ax_resources__.yaml 资源声明与编译期执行器白名单机制`](./0018-builtin-packages-and-executor-allowlist.md)

---

## 一、目标与问题边界

在 [RFC-0013](./0013-opaque-handle-type-and-resource-registry.md) 与 [RFC-0018](./0018-builtin-packages-and-executor-allowlist.md) 中，`type: handle` 被严格限制在工作流执行内部传递，所有外部边界（根工作流 `input`/`output` 与 `executor: signal` 的 `output`）均递归禁止包含句柄。当宿主将 Axcutor 封装为面向终端业务的可控工作流平台（例如直接传入已上传文件 `File`、图像 `Image`，或实时订阅并收回工作流产出的 `OutputStream`）时，若继续禁止外部边界出现 `handle`，业务包被迫为每种资源额外提供 `open_from_id` / `to_id` 字符串转换胶水节点，破坏了静态名义类型约束与图拓扑简洁性。

向宿主外部边界开放受管资源句柄，必须同时解决以下四个核心边界问题：

1. **区分“集群内跨 Worker 恢复”与“跨宿主边界公开寻址”**：
   * 在 [RFC-0013](./0013-opaque-handle-type-and-resource-registry.md) 中，Python `@resource` 实现 `locator` / `restore` 的初衷是支持集群内 Sticky 队列超时或 Worker 重启后的透明重连（例如携带内网数据库 DSN 的 `Conn` 或内网临时桶路径的 `ScratchBlob`）；
   * 这类内部定位符既不能允许外部不可信调用方通过 HTTP/CLI 任意构造注入（否则会引发 SSRF 或跨租户越权读取），也不能在外部轮询运行态栈帧或轨迹时明文泄露；
   * 只有资源包在 `__ax_resources__.yaml` 中**显式声明公开 `locator: TypeSpec`** 的资源类型，才构成对外公开可寻址契约。
2. **消除静态 YAML 与 Python `@resource` 的生命周期双真相源**：
   * 资源是否代表独立于单 Worker 存活的持久实体（`persistent=True/False`）以及如何析构（`dispose`），属于 Worker 侧 Python `@resource` 类的内部实现协议；
   * `__ax_resources__.yaml` 只应声明编译期、前端表单与宿主校验所需的静态接口类型（`description` 与 `locator`），不重复声明 `persistent`，从根源上消除静态头文件与运行期 Python 类之间的属性漂移。
3. **在五个外部可观测面上保持同构的闭合名义信封**：
   * 若在根 `input` / `output` 将句柄剥离为无名裸定位符（如 `{"file_id": "..."}`），会抹除名义类型 `kind`（导致共享相同定位符结构的 `File` 与 `Image` 在边界预检中互相混用），且与必须表达瞬态句柄（无定位符但非 `null`）的 `frames[].variables` 和 `trace.spans` 产生格式分裂，进而破坏 Playground 的内联输入校验与轨迹输入恢复；
   * 因此，所有外部输入、输出与只读观测视图必须采用统一的闭合名义信封，且其键集合与内部私有能力信封（`{"$handle": "<rid>", "kind": "..."}`）严格互斥。
4. **正交分离“Run 树驻留回收管辖权”与“外部持久实体删除权”**：
   * 宿主传入或根出口移交宿主的持久资源不能被工作流 GC 误删外部实体，但其在运行期间产生的 Worker 进程内存缓存与非持久本地连接仍须在根工作流不可达或结束时驱逐释放；
   * 这一宿主边界属性必须与父子 Workflow 之间的借用关系（`owned` vs `borrowed`）保持正交，确保外部句柄在跨多层独立 Child Workflow 借用、返回及取消竞态下不提前驱逐父缓存、也不因收尾竞态误判删除权限。

---

## 二、核心设计决策

### 2.1 `__ax_resources__.yaml` 公开 `locator` 声明与 `WorkflowBundle.resources`

1. **`__ax_resources__.yaml` 语法扩展**：
   ```yaml
   # delsey/resources/__ax_resources__.yaml
   resources:
     # 1. 未声明公开 locator：仅限执行内部传递；即使 Python 类实现了内部 locator/restore，
     #    也禁止出现在外部输入/输出边界，且快照与轨迹中隐藏其内部定位符。
     Conn:
       description: "集群内可重连会话句柄"

     # 2. 声明公开 locator：允许按闭合外部信封进出宿主边界，并在快照与轨迹中公开投影。
     File:
       description: "受管文件句柄"
       locator:
         type: object
         properties:
           file_id: { type: string }

     OutputStream:
       description: "追加式输出流句柄"
       locator:
         type: object
         properties:
           stream_id: { type: string }
   ```
   * 每个资源项的值可为 `null`，或仅允许包含可选字段 `description: string` 与 `locator: TypeSpec` 的闭合映射对象（**严禁在 YAML 中声明 `persistent`**，额外字段报 `schema.unknown_field`）；
   * `locator` 的静态良构约束：
     * 必须为合法 `TypeSpec`；
     * 必须非空（`locator.nullable == false`，否则在 `/resources/<Name>/locator` 报 `type.schema`），以保证可空句柄 `type: ["handle", "null"]` 的空值唯一由顶层 `null` 表达；
     * 必须为纯 JSON 值类型（`!locator.contains_handle()`，否则报 `type.schema`），定位符自身不嵌套其他受管句柄。
2. **AST、`ResourceSpec` 反序列化闭合与 `WorkflowBundle` 裁剪（`crates/axcutor-core/src/ast/mod.rs`）**：
   ```rust
   #[derive(Clone, Debug, Default, Serialize)]
   pub struct ResourceSpec {
       #[serde(default, skip_serializing_if = "Option::is_none")]
       pub description: Option<String>,
       #[serde(default, skip_serializing_if = "Option::is_none")]
       pub locator: Option<TypeSpec>,
   }

   #[derive(Clone, Debug, Serialize)]
   pub struct WorkflowBundle {
       pub entry: String,
       pub resources: Ordered<ResourceSpec>,
       pub workflows: Ordered<NormalizedAst>,
   }
   ```
   * **可达性裁剪**：与 `bundle.workflows` 仅收录自 `entry` 可达的工作流包一致，`bundle.resources` 仅保留 `bundle.workflows` 中实际引用到的 `HandleKind`（键为 `<ModulePath>:<ClassName>`），避免无关挂载包的资源声明膨胀运行期 Bundle 与 Event History；
   * **FFI 反序列化不变量**：`ResourceSpec` 的 `Deserialize` 实现（供 `validate_root_interface` / `validate_external_input` / `validate_external_output` 绑定使用）必须同步强制校验 `locator` 满足 `!t.nullable && !t.contains_handle()`，防止外部直接向校验绑定传入非编译器生成的畸形 `resources_json` 而放行 `{"$handle": K, "locator": null}`。

---

### 2.2 闭合句柄信封体系与外部边界契约校验

运行时与外部接口仅在静态 `TypeSpec` 为 `handle` 的位置识别句柄信封；普通 `object` / `map` 业务数据原样透传，不做无 Schema 猜测改写。非 `null` 句柄在内部与外部采用键集合严格互斥的闭合表示：

| 作用域与边界 | JSON 闭合结构 | 键集合与契约说明 |
| :--- | :--- | :--- |
| **1. 内部运行期能力信封**<br>*(Workflow `env` / Activity / Child IPC)* | `{"$handle": "res_018f...", "kind": "pkg:File"}` | 闭合双键 `{"$handle", "kind"}`。`"$handle"` 为私有运行期 `rid`，绝不接受外部输入，也绝不跨越查询或结果边界泄露。 |
| **2. 外部可寻址句柄信封**<br>*(根 `input` / 根 `output` / `signal` `output` / `frames` / `trace`)* | `{"$handle": "pkg:File", "locator": {"file_id": "f_1"}}` | 闭合双键 `{"$handle", "locator"}`。`"$handle"` 必须精确等于槽位声明的 `kind`，`"locator"` 必须满足 `bundle.resources[kind].locator` 的纯值字面量校验。 |
| **3. 不可寻址句柄诊断视图**<br>*(仅只读出现在 `frames` / `trace` 中)* | `{"$handle": "infra:Conn"}` | 闭合单键 `{"$handle"}`。用于未在 `__ax_resources__.yaml` 声明公开 `locator` 的瞬态资源或内部私有可恢复资源；既与 `null` 区分，又严格隐藏 `rid` 与内部敏感定位符。 |

#### 2.2.1 编译期、宿主边界校验与授权职责切分

1. **`executor: signal` 输出契约编译期检查（`compiler/schema.rs`）**：
   * 若 `signal` 节点的 `output` 中递归包含 `type: handle, kind: K`：当且仅当 `resources[K].locator.is_some()` 时允许编译通过；若 `resources[K].locator.is_none()`，编译期继续在对应 `/kind` 路径报告 **`type.signal_handle`**。
2. **根工作流接口检查（`validate_root_interface` & `"__main__"` 编译期检查）**：
   * 具名工作流包允许在顶层 `input` / `output` 中包含内部不可寻址句柄（供 `use: workflow:<PackagePath>` 内部调用）；
   * 当某工作流作为宿主根入口启动时，`validate_root_interface(input_schema, output_schema, resources)` 递归检查顶层 `input` 与 `output`：凡出现 `type: handle, kind: K` 且 `resources[K].locator.is_none()`，立即以输入校验错误拒绝启动；
   * 特例收敛：匿名入口 `"__main__"` 不可被其他工作流调用，编译器直接对 `"__main__"` 的顶层 `input` / `output` 执行同一检查，违例时在 `/input/.../kind` 或 `/output/.../kind` 报告静态诊断错误 **`type.root_handle`**（`target_kind = "variable"`），使编辑器在键入阶段即可感知。
3. **外部值入参/出参校验与解释器入口分流（`axcutor-core` / `axcutor-py` / `axcutor-wasm` / `workflow.py`）**：
   * 新增 `validate_external_input(input_json, schema_json, resources_json)` 与 `validate_external_output(response_json, output_schema_json, branches_json, resources_json)`：
     * 在非 `handle` 槽位完全沿用 `TypeSpec::accepts_literal`；
     * 在 `{ type: "handle", kind: K }`（或可空形式）槽位：若值为 `null` 则检查 `nullable`；否则要求值为仅含 `{"$handle", "locator"}` 两键的 JSON 对象，满足 `obj["$handle"] == K` 且 `resources[K].locator.accepts_literal(obj["locator"])`；
   * `AxcutorHost.submit`、Playground 提交与前端 `useRunInput` 统一调用 `validate_root_interface(..., resources)` 与 `validate_external_input(..., resources)`；
   * `AxcutorInterpreterWorkflow.run` 按 `parent` 严格分流：
     * `parent is None`（宿主根入口）：执行 `validate_root_interface` + `validate_external_input`，并将外部句柄信封物化进 `self._registry` 后构造根 `Frame`；
     * `parent is not None`（独立 Child Workflow）：继续执行 `validate_input`（内部能力信封校验）与 `self._registry.borrow(...)`；
   * `Signal` 投递与恢复交接（`deliver_signal` / `_wait_signal`）：
     * `validate_deliver_signal` 与 `deliver_signal` 统一调用 `validate_external_output`，并额外拦截当前已处于 `pending` 外部删除阶段（`entry.pending and not entry.descriptor["external"]`）的冲突定位符（校验不通过按 [RFC-0017](./0017-cooperative-signal-executor.md) 返回 `422 signal_validation_failed`，不污染 Event History）；
     * `deliver_signal` 写入 `waiter.response` 及返回给 HTTP 调用方的 JSON 均**保持已校验的外部闭合信封**（绝不在 Update 处理器中提前物化或返回内部 `rid`，避免泄露私有 `rid` 或在 `waiter` 等待唤醒期间因缺少 GC 根被并行分支回收）；
     * `_wait_signal` 在通过 `wait_condition` 与 `workflow.cancellation_reason()` 检查后、同步返回给 `_state_machine` 的 `_retain_result` 前（中间无任何 `await` 点），原子完成外部句柄物化与 GC 根挂接。
4. **公开 `locator` 的授权边界契约**：
   * 内核与解释器的 `validate_external_*` 保持纯确定性、无 I/O，仅验证名义 `kind` 与 `locator` 的静态值结构，**不隐含调用方或跨租户访问授权**；
   * 外部传入定位符的归属与访问权限，必须由宿主控制面在根提交 / `:signal` 入口预检完成，或由业务资源包将 `locator` 设计为自带密码学签名/不可伪造能力令牌并在 `@resource.restore(locator)` 中验签。

---

### 2.3 运行期物化、节点出口核验与正交所有权状态机

#### 2.3.1 正交双维度所有权模型（`entry.owned` $\times$ `descriptor["external"]`）

为避免将“跨父子 Run 的驻留协调责任”与“跨宿主边界的外部持久实体删除责任”混入单一布尔值或平面枚举，运行时将两者建模为两个正交布尔不变式：

1. **`ResourceDescriptor` 扩展与结构不变量**：
   * 字段定义：
     * `rid: NonEmptyString`
     * `kind: HandleKind`
     * `locator: JSONValue | null`
     * `persistent: bool | null`：由 Worker `resources.put()` 创建时为精确 `bool`（取自 `@resource(persistent=...)`，即使在根出口升格 `external=True` 后仍保持原 `bool` 值）；由宿主外部边界物化时**在描述符整个生命周期内恒为 `null`**（Worker 恢复实例时不向 Workflow 回写该字段，本地直接依据 `_protocol(cls)` 判定持久性）；
     * `creator_queue: NonEmptyString | null`：Worker 创建时为专属队列名；外部边界物化时恒为 `null`；
     * `external: bool`：`true` 表示底层持久实体（若存在）的所有权归属外部宿主（来自外部边界供应，或已在根出口锁定移交宿主），**Axcutor 任何时刻均不得对其执行外部持久实体删除**；`false` 表示由 Axcutor 内部创建且尚未移交宿主；
     * `resident_queues: List<NonEmptyString>`。
   * `description()` 强制结构不变量：
     * `(persistent is None) == (creator_queue is None)`；
     * 若 `persistent is None`，则必须满足 `external is True and locator is not None`；
     * 若 `persistent is bool`，则当 `persistent or external` 为真时必须满足 `locator is not None`。
   * 不可变身份与可变权限分离：
     * `same_identity(left, right)` 仅比对不可变身份字段（排除可变状态键 `"resident_queues"` 与 `"external"`），使根出口将内部描述符单向升格为 `external=True` 后仍能通过驻留 Worker 的身份一致性核验；
     * Worker 外部删除权限单向撤销：在 `ResourceStore` 中一旦收到 `descriptor["external"] == True` 或 `deleted == True`，本地条目的 `entry.descriptor["external"]` 单向置为 `True` 且 `entry.delete_external` 永久锁死为 `False`，任何迟到旧描述符或重复清理请求均不得重新授予删除权限。
2. **`RegistryEntry.owned: bool`（仅存于当前 Run 的 `ResourceRegistry` 内存账本）**：
   * `owned == true`：当前 Run 是该 `rid` 在执行树中的当前持有者，负责在该 `rid` 不可达或本 Run 结束时清理 `resident_queues`（或在其作为 Child 正常返回时移交父 Run）；
   * `owned == false`（即 **`borrowed`**）：该 `rid` 由父 Run 授权借入当前独立 Child Run；当前 Child Run **绝不自行发送 `dispose_resources`**，仅将运行期间新增的 `resident_queues` 通过返回外壳或收尾信号交回父 Run。

两个维度组合构成完备的四象限生命周期行为（注：`dispose_resources` Activity 的 `deleted` 参数表示“外部持久实体已无需删除/禁止删除”）：

| `entry.owned` | `descriptor["external"]` | 产生与流转场景 | 本 Run 不可达或退出时的回收行为 |
| :---: | :---: | :--- | :--- |
| `true` | `false` | 本 Run（或其已合并的子调用）通过 `resources.put()` 新建、尚未移交宿主或父 Run 的内部资源 | 向 `resident_queues` 发送 `dispose_resources(deleted=entry.deleted)`；若 `persistent == true` 且尚未确认删除，回退 `pool(creator_queue)` 执行 `restore=true, deleted=false` 幂等删除外部实体 |
| `true` | `true` | ① 本 Run 从外部定位符物化的资源；<br>② 子 Run 物化外部资源后随正常返回值上交本 Run；<br>③ 根 Run 锁定正常完成时将根 `output` 导出的内部资源单向升格为 `external=true` | 向 `resident_queues` 发送 `dispose_resources(deleted=true)`（关闭 `_protocol(cls) == false` 的本地连接并摘除 `_protocol(cls) == true` 的内存包装缓存），跳过 `creator_queue` 的 `restore=true` 回退，以 `set(resident_queues) <= entry.cleaned` 作为摘除条件 |
| `false` | `false` | 父 Run 将内部新建资源传入独立 Child Workflow 借用 | **不发送 `dispose_resources`**（防止提前析构父 Run 仍在使用的瞬态 `Tx` 或缓存副本），仅向父 Run 汇报累计 `residencies` |
| `false` | `true` | 父 Run 将外部供应资源传入独立 Child Workflow 借用 | **不发送 `dispose_resources`**，仅向父 Run 汇报累计 `residencies`，由父 Run 在全局不可达或根退出时按 `(owned=true, external=true)` 统一驱逐缓存 |

#### 2.3.2 公开 `locator` 即规范身份：`rid` 作为 Run 内唯一映射代理键（Canonical Surrogate Key）

对于在 `bundle.resources` 中声明了公开 `locator` 的资源类型（`bundle.resources[kind].locator is not None`），**`(kind, locator)` 本身即资源的规范身份**，而 `rid` 仅作为当前 Run 账本内映射到该 `(kind, locator)` 的唯一内部代理键。不可公开寻址的内部资源（含未在 YAML 声明公开 `locator` 的 `Tx` / `Conn`）仍保持每次 `put()` 独立的 `rid` 身份。

围绕这一原则，`ResourceRegistry` 在所有入口维持活跃条目中 `(kind, locator) <-> rid` 的严格一一对应：

1. **外部边界物化（根 `input` 与 `_wait_signal`）**：
   * 物化外部信封 `{"$handle": K, "locator": L}` 时，在当前 Run 的活跃（`not entry.pending`）条目中检索满足 `descriptor["kind"] == K` 且 `axcutor_core.literal_equal(descriptor["locator"], L)` 的条目：
     * **命中已有活跃条目 `rid_canon`**：直接复用 `rid_canon`，并**保持其原有 `descriptor["external"]` 不变**（若原为宿主供应的 `external=True` 则维持 `True`；若原为本 Run 内部新建的 `external=False` 草稿句柄经 `frames`/`trace` 观测后由 `:signal` 选回，则维持 `False` 不篡改删除责任，直至根 `output` 正常提交时才移交宿主）；
     * **未命中**：分配确定性 Run 内唯一标识（如 `f"res_ext_{run_id}_{seq}"`），登记 `descriptor(persistent=None, creator_queue=None, external=True, resident_queues=[])` 与 `RegistryEntry(owned=True)`。
2. **跨子调用 / Activity `created` 交接归并（`_receive_response` / `accept`）**：
   * 当独立卸载的 Child Workflow（例如子内通过 `:signal` 物化了父未传入的同一 `(K, L)` 并分配了 `rid_child`）或 Activity 返回 `created` 列表时，对其中声明了公开 `locator` 的条目检查当前父 Run 活跃账本（或同一批次先前条目）是否已存在同 `(K, L)` 的规范条目 `rid_canon`：
     * 若存在，将返回值 `result["output"]` 中对应句柄槽位的 `rid_child` 统一重写为 `rid_canon`，保持 `rid_canon` 原有的 `descriptor["external"]` 不变；
     * 若被归并的 `rid_child` 带有非空驻留队列（`resident_queues`），将其以 `RegistryEntry(descriptor={**d, "external": True}, owned=True, pending=True)` 挂入待清理队列，使紧随其后的 `_collect_resources()` 以 `deleted=True` 仅驱逐 Worker 上的重复包装副本，绝不误删底层持久实体。
   * 由此，无论 `use: workflow:*` 在进程内执行还是触发独立 Child Workflow 自动卸载，`builtin:compare`（`eq`/`ne`）对公开可寻址句柄恒等价于 `(kind, locator)` 判等，且同一底层实体绝不会因多重 `rid` 产生矛盾的外部删除指令。

#### 2.3.3 Worker 恢复核验与创建节点出口双端 Schema 校验

1. **Worker 恢复与本地析构判据（`ResourceStore`）**：
   * `ResourceStore` 统一以真实类协议 `persistent = _protocol(cls)`（而非可能为 `None` 的 `descriptor["persistent"]`）作为本地持久性判据：
     * 若 `descriptor["persistent"] is not None`，强制断言 `persistent == descriptor["persistent"]`；
     * 若 `descriptor["persistent"] is None`（外部边界物化描述符），强制要求 `cls` 定义了可调用的 `locator` 与 `restore` 钩子；
   * 首次恢复（`entry is None`）时调用 `cls.restore(locator)` 并通过 `literal_equal` 复核 `instance.locator()` 与 `descriptor["locator"]` 精确相等；
   * 当 `descriptor["external"] == True` 时，`entry.delete_external` 永久固定为 `False`；`_dispose` 仅在 `(not _protocol(type(entry.value))) or entry.delete_external` 为真时调用实例的 `dispose()`（即对外部持久资源仅摘除内存缓存引用，对外部非持久连接资源仍正常关闭本地连接）。
2. **创建节点出口按需下发与双端零信任 `locator` Schema 校验**：
   * **Worker 侧即时校验（按需下发，不膨胀纯值 Activity 载荷）**：工作流派发 `execute_run` Activity 时，仅当 `has_handles(body["output"])` 为真时，在 `resource_context["locator_schemas"]` 中按 `body["output"]` 实际涉及的 `HandleKind` 子集附带公开 `locator` `TypeSpec`。`ResourceContext.exported` 对本次新分配且导出的 `rid` 强制校验 `descriptor["locator"] is not None` 且满足对应 `locator` `TypeSpec`；违例时当场抛出契约异常（映射为节点 `contract` 错误），未移交实例留在孤儿集合中由当前 Attempt 的 `finally` 立即在本地执行 `dispose()` 清理；
   * **Workflow 侧零信任复核**：`_receive_response` 在接收 Activity 或 Child 返回的 `resources["created"]` 时，同步用 `self._bundle["resources"]` 复核每个新建公开可寻址资源的 `descriptor["locator"]` 满足静态 `locator` `TypeSpec`（违例抛 `Recoverable("contract", ...)`），确保自定义 Executor 亦无法绕过解释器契约边界。

#### 2.3.4 根出口去物化与消除权限竞态的收尾顺序

在 `AxcutorInterpreterWorkflow.run` 中，当 `parent is None` 时：
1. **构造独立外部结果副本（保留内部 `_resource_root` 直至最终交接）**：
   * 主体执行正常返回后，计算 `exported = set(scan(result["output"], root["output"]))`；
   * 通过 `dematerialize_external_outputs` 生成**独立的外部结果字典 `external_result`**（不原地改写 `result["output"]`，也不提前调用 `_release_result(result)`，从而保证在 `finally` 排空处理器期间，在途 `resource_update` 触发的 `_collect_resources()` 仍能正常 `scan` 并保护 `exported`），并对 `external_result` 执行 `validate_external_output` 复核（违约则清空 `exported` 并转为 `Recoverable("contract", ...)` 失败路径）。
2. **`finally` 有界收尾阶段（共享 `expires = workflow.time() + CLEANUP_SECONDS` 预算）**：
   * **第一步（排空内部处理器与冷启动定时器）**：若已进入失败或取消态（`failure is not None or workflow.cancellation_reason() is not None`），立即清空保护集 `exported = set()`；等待 `workflow.all_handlers_finished()`（受 `expires` 约束）排空在途 Update/Signal 处理器，并取消排空 `_signal_init_timer`；
   * **第二步（清理未导出内部资源）**：以当前 `exported` 作为保护集调用 `_collect_resources(final=True, exported=exported, expires=expires)`；
   * **第三步（裁决最终完成态 vs 取消/失败态，释放 `_resource_root`）**：
     * 调用 `self._release_result(result)`（若存在）；
     * 若此时 `failure is not None` 或 `workflow.cancellation_reason() is not None`：标记工作流为失败/取消，若此前 `exported` 非空（即取消发生在第二步期间），保持其 `external=False` 并再次调用 `_collect_resources(final=True, expires=expires)` 按 [RFC-0013](./0013-opaque-handle-type-and-resource-registry.md) 的有界尽力清理契约执行外部删除（超时未确认项记录 `unconfirmed` 告警并由底层存储 TTL 兜底），随后抛出对应异常；
     * 若此时 `failure is None and workflow.cancellation_reason() is None`：**正式锁定成功完成路径（后续不再响应取消改判或重新抛出 `CancelledError`）**：
       1. 将 `exported` 中所有条目的 `descriptor["external"]` 单向置为 `True`；
       2. **同步覆盖历史调用记录**：遍历 `self._resource_calls.values()`，将 `exported` 中涉及的 `call["allowed"]` 描述符同步标为 `external=True`（并记入 `call["deleted"]`），防止第三步等待期间迟到送达的 `resource_update` 信号用旧快照重建出 `external=False` 条目而误删已移交宿主的外部实体；
       3. 调用 `_collect_resources(final=True, expires=expires)` 以 `deleted=True` 驱逐各驻留 Worker 的缓存副本，随后直接 `return dumps(external_result)`。

---

### 2.4 快照、轨迹与 Playground 全链路对齐

1. **写入时即时观测投影 `ResourceRegistry.project_values(values, symbols, resources)`**：
   * 按 `symbols` 递归扫描 `values` 中的 `handle` 槽位：
     * 若 `resources[kind].locator` 存在，投影为 `{"$handle": kind, "locator": entry.descriptor["locator"]}`；
     * 若 `resources[kind].locator` 为 `None`，投影为 `{"$handle": kind}`；
   * 投影必须在**写入快照或记录轨迹当时即时完成**（避免后续查询历史轨迹时因 `rid` 已被 GC 摘除而丢失定位符），并完整覆盖全部业务载荷写入路径及其精确 `TypeSpec` 映射：
     * `get_runtime_snapshot`：每个活跃 `Frame` 按 `frame.symbols` 投影 `frame.env`；
     * 宿主根 `"workflow"` Span（`parent is None`）：直接记录入口已校验的外部输入与出口去物化后的 `external_result`；
     * 独立 Child `"workflow"` Span（`parent is not None`）：按被调根工作流的 `root["input"]` 与 `root["output"]` 投影（防止 Playground 合并子工作流轨迹时泄露内部 `rid`）；
     * `builtin` 节点输入：根据 `body["bindings"]["input"]` 中 `"path"` 绑定经 `_path_type(symbols, arg["path"])` 推导算子形参 `TypeSpec` 映射后，经 `_trace_input` 投影写入 `span["input_json"]`（`_trace_result` 按 `_output_types(body, symbols)` 投影）；
     * `workflow` 子调用节点：`_trace_input` 按被调方 `root["input"]` 投影，`_trace_result` 按调用方 `_output_types(body, symbols)` 投影；
     * `for_each` 外层聚合 Span：`_trace_call` 与 `_trace_result` 统一按外层 `frame.symbols` 投影；其余 `run` / 内联 `graph` 节点按各自 `input_schema`（或组装输入符号表）与 `_output_types` 投影。
2. **Playground 前后端闭环（`playground/{backend,src}/`）**：
   * 后端 `PlaygroundCoordinator.submit` 切换至携带 `bundle["resources"]` 的 `validate_root_interface` 与 `validate_external_input`，并在刷新 `self._module` 挂载表时同步更新 `self._worker.store.mounts`；
   * 前端 `WorkflowBundle` TypeScript 接口补充 `resources: Record<string, ResourceSpec>`；
   * `defaultInput(schemaRaw, resourcesRaw)` 对声明了公开 `locator` 的 `handle` 生成默认外部信封骨架 `{"$handle": kind, "locator": <locator 默认字面量>}`（对不可寻址句柄生成 `null` 交由预检拦截）；
   * `useRunInput` 将 `resourcesRaw` 一并纳入未编辑草稿的刷新依赖键与实时校验计算属性（调用 WASM `validate_root_interface` 与 `validate_external_input`）；从 `trace` 根 Span 恢复历史输入时无需任何格式改写即可直接再次运行。

---

## 三、变更责任边界

| 模块路径 | 设计责任 |
| :--- | :--- |
| `crates/axcutor-core/src/ast/mod.rs` | 新增 `ResourceSpec { description, locator }`（含反序列化非空纯值校验），并在 `WorkflowBundle` 中持久化可达 `resources: Ordered<ResourceSpec>`。 |
| `crates/axcutor-core/src/compiler/{mod,schema}.rs` | 解析 `__ax_resources__.yaml` 的非空纯值 `locator`；裁剪可达 `bundle.resources`；放宽具公开 `locator` 句柄在 `signal` `output` 与 `"__main__"` 根接口（`type.root_handle`）上的编译期检查。 |
| `crates/axcutor-core/src/kernel/mod.rs` & `axcutor-{py,wasm}` | 提供 `validate_root_interface(..., resources)`、`validate_external_input`、`validate_external_output` 与定位符字面量校验绑定。 |
| `python/axcutor_runtime/src/axcutor_runtime/{resource_model,resources,worker,workflow}.py` | 实现 `(owned, external)` 正交所有权模型、公开 `(kind, locator)` 规范映射与跨调用归并、双端 `locator` 校验、防迟到信号复活删除的根出口交接，以及全路径 `frames` / `trace` 即时投影。 |
| `python/axcutor_host/src/axcutor_host/host.py` | 提交预检切换至携带 `bundle.resources` 的 `validate_root_interface` 与 `validate_external_input`。 |
| `playground/{backend,src}/` | 后端提交预检与 `store.mounts` 同步；前端 `compiler.ts`、`runInput.ts`、`useRunInput.ts` 接入 `bundle.resources` 骨架生成、草稿缓存键与内联校验。 |
| `manual/{spec,docs}/` | 同步更新资源头文件语法、外部闭合句柄信封规范、信号/根边界授权契约及 HTTP API `Execution` / `Frame` 文档。 |
