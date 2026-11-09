# RFC-0013: 不透明句柄类型与跨 Activity / Workflow 资源生命周期

* **状态**：Implemented
* **范围**：类型系统扩展、跨 Activity / Child Workflow 资源传递、Worker 亲和性与确定性资源回收。
* **关联提案**：[RFC-0009](./0009-static-subtyping-and-covariance.md)、[RFC-0011](./0011-compiler-and-temporal-runtime-architecture.md)、[RFC-0012](./0012-run-executor-and-builtin-python.md)、[RFC-0014](./0014-module-python-executor.md)、[RFC-0015](./0015-unified-package-and-import-system.md)、[RFC-0016](./0016-activity-heartbeat-configuration-and-throttling.md)、[RFC-0017](./0017-cooperative-signal-executor.md)。

## 一、目标与边界

`handle` 表示不能内联为普通 JSON 的受管资源。图内仍只有 `graph.variables`，不增加资源变量表。设计采用以下边界：

1. **每个资源有明确的所属 Run**。内联子图与进程内子工作流共享 Registry；独立 Child 可以借入已有资源，并把新产出的资源移交父 Run。框架透明管理这些边界。
2. **句柄只表达身份**。公开信封只有资源 ID 与名义类型；恢复、路由和清理描述只保存在运行时资源表中。
3. **业务创建和操作对象，框架管理其持有与回收**。公共资源接口只有 `put(instance)` 与 `get(handle)`；事务提交等操作由资源类定义。
4. **最后引用消失即安排回收**。在状态提交点扫描确定性的执行根集合，及时释放循环中不再可达的连接、事务和临时文件，不维护逐槽位引用计数。
5. **失联不永久阻止恢复与回收**。仍有效的逻辑引用和在途调用决定资源是否存活；失联旧调用在有界收尾后结束调度授权，本地实际使用者仍受 Store 保护。业务负责并发访问、跨 Worker 重试的幂等与迟到操作约束。
6. **回收是有界尽力清理**。清理失败保留责任与诊断，不改变业务异常。外部临时对象必须有 TTL 兜底，框架不承诺崩溃窗口内绝对无泄漏。

### 1.1 三类资源共用一个协议

`locator` / `restore` 决定能否在其他 Worker 恢复访问；`persistent` 标记是否存在独立于 Worker 存活、由框架承担删除责任的外部对象。无 locator 包括未定义定位协议或 locator 返回 None；这类实例必须为 `persistent=False`。

| 类别与典型资源 | `locator` / `restore` | `persistent` | 受管状态与恢复语义 | dispose 的职责 |
| :--- | :--- | :--- | :--- | :--- |
| 瞬态资源，如数据库事务 `Tx` | 无 | `False` | 绑定原 Worker 的物理实例，不能重建原会话状态 | 回滚并关闭原实例持有的连接 |
| 可恢复非持久资源，如无连接本地状态的 `Conn` | 有 | `False` | 每个 Worker 可有自己的物理实例，恢复等价访问能力 | 关闭各存活缓存连接；不为清理新建连接 |
| 可恢复持久资源，如外部临时文件 `TempFile` | 有 | `True` | 外部实体独立存活，恢复对同一实体或精确版本的访问 | 幂等删除外部实体，必要时在其他 Worker 恢复后删除 |

表中的回滚、关闭和删除均由资源类实现。正常回收会调用已定义的 dispose；非 persistent 类未定义该方法时只摘除实例，需要显式释放连接等资源的类应实现 dispose。persistent 类必须提供非空 locator、restore 与 dispose。

定位符必须足以恢复同一逻辑实体或等价访问能力。仅有 DSN 的连接不能恢复临时表、会话设置或未提交事务；依赖这些状态的资源应使用瞬态句柄。

资源的并发访问由业务和资源类保证，框架不声明或检查访问模式。同一 rid 在一个 Worker 上可能命中同一实例，在不同 Worker 上可能恢复出不同实例；业务须按底层连接或外部系统的约束安排访问。

`persistent=True` 的实例只保存定位信息或借用 Worker 管理的客户端，不跨 Activity 长持需要显式关闭的本地连接、文件或流。每次业务操作自行关闭临时打开的本地资源；因此 Worker 摘除这类缓存不需要额外析构钩子。

#### 1.1.1 创建与所有权交接

三类资源遵循相同流程，详细交接协议见第 5.1 节：

1. **业务创建、尚未 put**：实例及已创建外部对象的清理责任由业务承担；put 校验失败不构成接管。
2. **put 成功**：真实实例进入 Worker Store，当前 Attempt 在内存登记 allocated_rids 和清理责任；此时尚未向 Workflow 移交该资源。
3. **Activity 成功且全部校验通过**：完整输出中的新资源移交当前 Run 的 Registry；未导出的新资源在 Attempt 本地回收。调用方映射后丢弃的资源由 Registry 按不可达规则回收。
4. **Activity 失败、超时、取消或契约失败**：全部 allocated_rids 进入本地孤儿清理；协作失败在业务排空及本轮有界清理后继续原有跳过、retry 或 catch。获准输入不属于孤儿，仍由原所有者决定何时销毁。

跨 Child Run 的借入与移交不改变资源类别：输入保留原所有权，新创建且成功导出的 owned 资源移交父 Run。真实实例仍留在对应 Worker，具体规则见第 1.2 节。

#### 1.1.2 正常与异常情况下的行为

下表以资源已经移交所属 Run、Registry 已掌握规范描述为前提。恢复实例只恢复访问能力，业务是否重试仍按 DSL 规则决定。

| 场景 | 无 locator，persistent=False | 有 locator，persistent=False | 有 locator，persistent=True |
| :--- | :--- | :--- | :--- |
| 后续 Activity 访问 | 必须回原 Worker 取得同一实例；实例丢失则报 execution | 优先使用缓存；未命中时恢复等价实例 | 优先使用缓存；未命中时恢复同一外部实体的访问 |
| 最后引用消失，或所属 Run 正常退出 | 原 Worker 在本地使用者排空后 dispose | 各登记驻留 Worker 在本地使用者排空后 dispose；不为清理恢复新实例 | 幂等删除外部实体并摘除本地缓存；必要时在共享队列 restore 后 dispose |
| Activity Worker 正常停机 | 排空 Activity 后尝试 dispose，原句柄随后无法恢复 | 关闭本地实例，后续访问可在其他 Worker 恢复 | 摘除本地缓存，保留仍由所属 Run 持有的外部实体 |
| 原 Worker 崩溃或被强制终止 | 无法补执行原实例的 dispose，后续访问失败；远端连接或事务依赖断连、服务端超时等机制收尾 | 后续可恢复新实例，不能靠新实例释放旧连接；旧连接依赖进程退出、断连及服务端机制释放 | 外部实体仍存在时可恢复访问；达到回收条件后可在其他 Worker restore 后 dispose |
| 调用已启动后超时、取消或失联 | 有界收尾后，后续访问仍须回原 Worker；实例丢失则失败 | 有界收尾后，后续调用可在其他 Worker 恢复；旧副本仍保留已知清理责任 | 有界收尾后允许恢复访问；达到回收条件时允许跨 Worker 删除，不无限等待失联任务 |
| Sticky 排队 5 秒超时，输入只有该类资源 | 报 execution，不回退共享队列 | 可回退共享队列 | 可回退共享队列 |
| restore 失败 | 不适用 | 本次访问失败，半构造对象由资源类释放 | 本次访问或清理失败，半构造对象由资源类释放；未完成的外部删除仍为未确认 |

有界收尾不证明旧任务已经停止。原 Worker 的本地使用记录保留至实际排空，保护本地实例不被提前析构；persistent 外部实体在其他 Worker 被删除后，旧任务可能访问失败。并发访问、重试幂等及迟到操作安全仍由业务和资源类保证，新建外部产物应使用独立代次或精确对象版本。

以下异常边界适用于三类资源：

* **本地孤儿清理失败**：每个孤儿本轮最多尝试一次 dispose；记录诊断、保留待回收条目并继续其余项及原业务结果。排空和本轮清理共用 30 秒上限，条件满足即开始，不等待窗口结束。后续正常收尾或 Worker 停机再尽力处理，不新增后台重试服务或持久清理队列。
* **put 后、移交前 Worker 崩溃**：Attempt 内存账本随进程丢失，Workflow 尚无规范描述，无法据此恢复或补做清理。进程内资源依赖进程退出及远端释放机制，独立存活的外部对象依赖 TTL。仅具备 locator 不能消除这一窗口。
* **成功响应的交接结果不确定**：输出可能已由接收者持有，创建方不能擅自删除导出资源。已知且可寻址的责任继续自动处理，未确认部分依赖 Worker 退出或外部 TTL；不能用立即删除换取可能已移交资源的丢失。

### 1.2 与 Child Workflow 的边界

`use: workflow:*` 保留 RFC-0011 中按历史长度自动派生 Temporal Child Workflow 的规则，含句柄的输入、输出及内部资源不阻止该调度。用户无需自行控制 Run 历史预算，也无需为切换 Run 把句柄改成业务定位符。

* 进程内调用共享当前 Registry。独立 Child 使用自己的 Registry，把入口资源登记为 borrowed；父调用在 Child 存续期间保留对应执行根及在途使用记录，借入资源不因 Child 局部不可达而被销毁。
* 每个 Run 内新创建的资源为 owned。Child 正常返回时，将完整输出中新创建的 owned 资源及其规范描述、驻留责任移交父 Run；借入句柄返回时仍沿用原所有权。
* 父 Run 先接管完整返回值，再做 `with.output` 映射。未映射的新资源由父 Run 正常回收，Child 已移交的资源不重复析构。Child 未导出的新资源在自身退出时回收。
* 结果、普通失败及收尾更新携带借入资源的累计驻留信息，不依赖业务输出是否包含句柄。多层 Child 沿直接调用关系交接；根 Run 对宿主仍只返回纯值。

句柄跨 Run 传递的是身份、描述和授权，真实瞬态对象仍驻留于原 Worker；切换 Child 不把内存事务变成可恢复资源。不增加跨独立根执行共享资源的服务。

## 二、类型与外部边界

### 2.1 `TypeSpec`

在现有六种 JSON 基础类型之外增加 `type: handle`：

```yaml
# kind 区分事务与临时文件；访问顺序由业务安排。
tx:
  type: handle
  kind: infra.postgres:Tx

file:
  type: [handle, "null"]
  kind: infra.s3:TempFile
```

`handle` 只允许 `type`、`kind` 与 `description` 字段。`kind` 必填；可空性、数组、映射及结构体组合沿用已有规则。

`kind` 的文法为 `<ModulePath>:<PublicClassName>`：模块路径复用 RFC-0015 的 `package_path`，类名复用 `package_segment` 且不得以 `_` 开头。模块中的私有段沿用工作流包可见性规则；不增加 kind 前缀继承。

同为句柄的类型在满足原有可空性规则时：

```text
T1 <: T2  iff  T1.kind == T2.kind
```

等价要求两者的可空性与 kind 分别相等。编译器只检查 YAML 声明，不导入 Python 类；Worker 在资源交接和解引用时核对资源类及其协议。

### 2.2 纯值行为与字面量

* 句柄是原子类型，禁止字段投影、算术、逻辑及有序比较；允许纯值算子复制、放入容器、从容器取出及 `unwrap`。
* `compare` 的 `eq/ne` 沿用现有 `literal_eq`。同 kind 的合法句柄只有 rid/kind 两字段，信封相等即资源 ID 相等；容器的现有递归比较也自然适用，无需增加按类型分派的比较器。编译器仍禁止不同 kind 的句柄相互比较。
* 编译期 `accepts_literal` 递归拒绝句柄位置的非空字面量；可空位置的 `null`、空数组和空映射仍合法。
* 运行期 `accepts_runtime` 校验信封形状与预期 kind。资源属性、来源和访问权限由 Registry / ResourceContext 校验，形状校验本身不授予资源权限。

资源扫描由 `TypeSpec` 引导，仅在 `handle` 类型位置识别信封。普通 `map<string>` 中的 `{"$handle": "customer-value"}` 仍是业务数据。

### 2.3 所有宿主入口保持纯值

1. **根启动接口**：实际作为根入口启动的工作流，其 input 与 output 递归禁止 `handle`，包括可空句柄及容器。通用定义编译和契约生成仍允许含句柄接口，供执行内部调用。
2. **外部恢复接口**：`executor: signal` 的 output 递归禁止 `handle`，覆盖根 run、图内节点和被调用工作流；编译期检查，并在 Update 的只读 validator 中复核。signal 的输入仅作依赖，不解引用资源。
3. 提交前由宿主调用独立的根接口校验，Workflow 根据真实父执行信息再次识别根入口并校验；外部不能用伪造的调用深度绕过限制。
4. HTTP、CLI、宿主结果及 signal 投递继续使用原有 JSON 协议；不增加宿主资源管理接口。诊断快照可展示句柄身份，但不能凭它取得资源。

## 三、Python 资源协议

### 3.1 类声明与寻址

```python
from axcutor_runtime import resource

@resource
class Tx:
    # 不定义 locator / restore：实例只存在于创建它的 Worker。
    async def dispose(self) -> None:
        ...

@resource(persistent=True)
class TempFile:
    def locator(self) -> dict[str, str]:
        ...

    @classmethod
    def restore(cls, locator: dict[str, str]) -> "TempFile":
        ...

    async def dispose(self) -> None:
        ...
```

`@resource` 与 `@resource(...)` 均可用，选项只有 `persistent: bool = False`。类必须为可按 kind 导入的公开模块顶层类；未直接装饰的子类不是资源类。

| 方法 | 要求 | 职责 |
| :--- | :--- | :--- |
| `locator(self)` | 与 restore 成对；persistent 类必填 | 返回可传输定位符；返回 None 的实例是瞬态资源 |
| `restore(cls, locator)` | 类方法或静态方法；persistent 类必填 | 在新 Worker 构造实例，不重新创建外部实体 |
| `dispose(self)` | persistent 类必填，其余可选 | 释放本实例资源，并按需幂等删除外部实体 |

方法均支持同步返回或 awaitable。`restore` 必须返回精确的声明类实例。`persistent=True` 时 locator 不得为 `None`；restore 应允许外部对象已不存在，缺失判定和幂等处理由 dispose 完成，框架不按通用异常类型猜测删除成功。

资源类复用 RFC-0015 的 `module_mounts`：

* 正向按逻辑模块路径导入资源类；反向从实例所属模块推导逻辑 kind。挂载必须可唯一逆映射，沿用现有禁止目标重叠与别名的规则。
* 必须满足 `resolve_resource_class(kind) is type(instance)`，拒绝跨模块 re-export 别名、嵌套类和未装饰子类。
* 模块加载与 module 执行器共用包/文件无二义性校验。Worker 间只传 JSON 描述，不传类对象；同一 kind 必须部署兼容协议，破坏性变更使用新 kind 或固定部署版本。

### 3.2 定位符沿用无损 JSON

locator 使用仓库现有的 `codec.dumps/loads` 深拷贝、校验并传输，保留大整数、十进制数和对象顺序。Rust 复用现有 JSON 解析与 `literal_eq` 的精确数值相等性，不另设安全整数范围或 f64 转换规则。

`restore` 接收的是 codec 解码后的 JSON 值，可能包含 `Decimal` 或大整数的传输表示，不保证原生 float 类型；资源类在调用具体 SDK 前自行适配。非有限数、非字符串对象键及不能表示为 JSON 的对象被拒绝。

定位符不承载其他受管资源的所有权或依赖；框架不扫描 locator 寻找资源，也不根据其中的键名识别句柄。注册快照与交给 restore 的值彼此隔离，业务不能通过修改原定位符改变已登记的身份。

### 3.3 公共接口

```python
from typing import TypeVar
from axcutor_runtime import Handle, resources

T = TypeVar("T")

class ResourceContext:
    async def put(self, value: T) -> Handle[T]:
        """注册资源实例；kind、固有属性与 locator 均由类协议推导。"""
        ...

    async def get(self, handle: Handle[T]) -> T:
        """在当前 Attempt 的授权范围内取得实例，并保活至实际排空。"""
        ...
```

`put` 完成类与定位符校验后才接管实例；此前由调用者负责释放。提交注册时原子检查同一 Python 实例尚未注册，分配全局唯一 rid，并将 Store 条目与当前 Attempt 的内存清理责任一并登记，再返回 Handle；这一提交过程不挂起，并发 put 只能有一次成功。返回前发生取消也不会留下已入 Store 却未登记责任的资源。同一实例以及同一底层连接、事务或流不能被注册成多个资源；不维护跨 rid 依赖图。

对 persistent 资源，put 转交外部对象或精确对象版本的唯一清理权：同一个删除单元只能属于一个 rid，恢复副本必须沿用该 rid。禁止用两个不同 Python 包装实例重复注册同一对象，也禁止另一个 Run 再取得该对象的独立删除权。这是资源类和调用者的前置条件；框架不增加全局 locator 去重服务。仅借用外部对象且不持有删除权的包装，dispose 不得删除该对象。

`get` 只接受公共 `Handle`，核对当前 Attempt 的输入或分配白名单。缓存未命中时，瞬态资源报 `execution`，可恢复资源按授权描述恢复；恢复失败由资源类释放半构造对象。命中与恢复均检查精确类身份、协议属性和本地回收状态。

`get` 返回的实例只借用到当前 Attempt 排空为止，不能存入全局变量或游离任务跨 Attempt 使用。资源钩子执行期间禁用 resources 上下文，避免递归分配和借用。Attempt 内的协程顺序由业务负责。

业务通过实例方法执行提交、删除等需要感知成败的操作；`dispose` 必须能重复调用，事务类自行记录是否已完成。框架不提供绕过析构的 discard 或跨别名关闭句柄的 close；业务对象发生何种外部状态变化，不转换成另一套句柄终结同步协议。

## 四、运行时数据模型与 Worker 路由

### 4.1 三种表示各有唯一职责

公开句柄的闭合信封为：

```json
{"$handle": "res_018f...", "kind": "infra.postgres:Tx"}
```

两字段都是非空字符串，kind 遵守前述文法，不允许额外字段。公共 `Handle[T]` 是冻结模型，按 rid 相等及哈希；泛型仅用于静态类型检查。资源路由或恢复信息不放入业务可见句柄。

| 位置 | 保存内容 | 权威范围 |
| :--- | :--- | :--- |
| Workflow Registry | rid、kind、locator、persistent、创建队列、驻留队列、owned/borrowed 所有权、在途调用、未确认使用及清理责任 | 当前 Run 的身份、调度与逻辑持有；所属 Run 负责最终回收 |
| Worker ResourceStore | rid 对应的真实实例、规范描述、本地使用者及回收状态 | 当前进程的物理对象 |
| Attempt ResourceContext | 此次获准输入的描述、本地使用记录及新分配资源的内存清理责任 | 此次调用的资源访问与交接前回收 |

创建队列固定为 `<base_queue>-worker-<32 位小写 hex>`，按末尾固定后缀解析所属池；驻留队列是缓存与清理责任记录，不是存活证明。

请求在内部 `resource_context` 中只携带获准输入的描述；rid/kind 必须与业务输入一致。未知 ID 不得凭 locator 或信封形状注册进 Registry。资源在 put 时形成规范身份，后续返回不能改写它；只有驻留信息可以合并。

### 4.2 Sticky 路由

目标 executor 的 base_queue 为 `options.task_queues.get(executor) or workflow.info().task_queue`。Worker 进程同时监听共享 base_queue 与独有 worker_queue，共享同一个 Store、挂载表和 Activity 实现；主队列注册 Workflow 与 Activity，专属队列只注册 execute_run、dispose_resources Activity。

1. 有瞬态输入时必须路由到其创建队列；来自不同 Worker 或不属于目标 executor 池的瞬态输入报 `execution`，不覆盖 executor 路由。
2. 只有可恢复输入时，在目标池优先使用该 Run 最近成功使用的 Sticky 队列，其次选输入描述中确定性顺序的兼容驻留队列，最后使用共享队列。
3. Sticky 任务尚未领取即排队超时，且全部输入可恢复时，可以回退共享队列。瞬态输入报 `execution`；两者都保留原队列的清理责任。
4. 无句柄输入且输出 schema 也不含句柄的节点直接走共享队列。可能创建句柄的节点可使用已有亲和性，便于多个瞬态资源同域创建；不保证并发创建一定同域。
5. 已启动 Activity 的失联按第 5.4 节处理，不能当作未领取任务透明重投。

路由层在通用异常映射前识别 SDK 的 schedule_to_start_timeout，以分别执行回退或瞬态路由失败。排队时间独立于从 Activity 启动时起算的业务 timeout；外层复合节点若设置总期限，仍约束整个子调用。

Sticky 排队期限固定为 5 秒；取消后的排空等待及单轮清理共用 30 秒总预算，属于内部常量，不增加 options 或 DSL 配置项。清理条件满足即开始，30 秒是上限，不是延迟窗口；各项清理共用本轮剩余预算，失败不续期。业务 deadline、Activity start_to_close_timeout、心跳与节流遵守 RFC-0016；不为句柄调用额外拼接收尾时长。预算结束仍未排空时，保留 Worker 本地使用记录，Workflow 按第 5.4 节结束旧调度授权并继续恢复或回收。

`create_worker` 返回复合 Worker，公开 run、shutdown 与异步上下文管理。任一成员启动失败时关闭已启动成员；正常停机先停止接收、排空 Activity，再关闭 Store。非 persistent 条目尝试 dispose；persistent 条目只摘除内存引用，外部实体由所属 Run 的回收或 TTL 清理。

## 五、持有、在途使用与回收

### 5.1 Attempt 向 Registry 交接

资源在成功交回前由创建它的 Attempt 负责。上下文在内存维护 `allocated_rids`；每次 put 与 Store 注册一并登记新 ID，无需先向 Workflow 持久登记：

1. 执行器返回后，先完成分支选择、契约投影与来源校验，再按已校验的完整输出扫描 `exported_rids`。输出 ID 只能来自获准输入或本 Attempt 分配；新资源必须仍在 Store，kind 和类属性与登记一致。
2. Attempt 的 finally 负责本地孤儿清理：成功且全部校验通过时，清理 `allocated_rids - exported_rids`；业务失败、契约失败、超时或取消时，清理全部 `allocated_rids`。协作失败路径先完成这一轮有界清理，再返回原失败供 Workflow 处理跳过、retry 或 catch。输入资源不属于孤儿；成功返回后发生的交接不确定性另按第 5.4 节处理。
3. 全部 execute_run Activity 统一返回以下两部分，经 codec 编码为 JSON 字符串。先接管资源并将完整输出加入待提交根，再执行调用方的输出映射；未映射输出失去可达性后回收。

```text
{
  result: {branch, output},
  resources: {
    created: [新导出资源的完整描述],
    worker_queue: 当前 Worker 队列,
    resident_rids: [获准输入及新导出资源中，仍驻留于该 Worker 的 ID]
  }
}
```

4. 普通业务异常或契约失败在业务实际排空、本轮本地清理完成或达到预算后，通过原选择器的 `ApplicationError` details 交回驻留信息；不移交失败 Attempt 的新分配所有权。父 Workflow 在 retry / catch 前合并这些信息。
5. 心跳保持故障检测与取消送达职责，不携带资源账本。取消或完成响应已无法送达时，Worker 可在实际收尾后向直接调用的确切 Run 发送一次最终 resource_update，补交驻留和清理责任；它不重新提交业务结果，也不是恢复或回收的等待屏障。

本地清理先取消并排空业务及其派生任务，再释放本 Attempt 对孤儿的本地使用记录，直接调用 dispose，不另调度清理 Activity。输入及成功导出资源的本地使用记录保留至收尾结束。清理不随业务取消一并取消，只使用本轮剩余预算；若业务仍未实际排空，保留使用记录和待回收标记，由最后使用者的收尾继续驱动清理。

每个孤儿在本轮最多尝试一次 dispose。清理抛错时记录 rid、kind 和错误，继续处理其余项；失败或预算耗尽的条目留在 Store 的待回收状态，沿用后续正常收尾及 Worker 停机时的尽力处置。清理失败不替换原异常、不改变跳过或 retry/catch 选择，也不把成功结果改为失败；不增加独立后台重试、持久清理队列或额外交接协议。进程崩溃或强制终止可能绕过 finally，仍由进程退出和外部 TTL 覆盖。

纯值 Activity 也使用相同包装，资源列表为空；Workflow 统一解包后再校验业务契约。自定义 Executor 仍只返回 `{branch, output}`，资源元数据由 Activity 包装层形成。根 Workflow 对宿主仍返回原有 `{branch, output}` JSON 字符串；不提供两种 Activity 返回形状或旧版兼容分支。

独立 Child 的内部成功响应沿用 result/resources 结构：created 表示本次向父移交的新 owned 资源，驻留信息累计覆盖各 Worker，不传局部可达集合。普通失败只交回 borrowed 资源的驻留更新，新资源由 Child 本地所有权回收。取消或无法返回时，Child 可向直接父 Run 发送一次累计收尾更新；父不轮询 Child 心跳，也不等待该更新才进入有界收尾。

收尾更新绑定确切来源 Run 与调用 Attempt，重复应用无副作用。迟到业务输出不重新生效；仍活跃资源的驻留提示可合并，已进入回收的资源只追加清理责任，不能因迟到更新重新激活。正常返回路径不发送额外 Signal，单次兜底发送失败由诊断、Worker 退出及 TTL 覆盖。

### 5.2 确定性根集合扫描

Registry 不存 ref_count。运行时登记三类根，在状态提交后按 TypeSpec 扫描得到可达 ID 集合：

| 根 | 存续范围 |
| :--- | :--- |
| 实际执行环境 | 从建帧至退帧；变量覆写和 kill_vars 直接反映在当前 env |
| 待提交结果 | 从生产方交回至投影、写回或丢弃；包含并发屏障中已完成分支的结果 |
| 在途调用输入 | 从发起调用前至正常收尾；独立 Child 的输入由父调用持有，失联后按有界授权结束规则处理 |

扫描只求集合，不按出现次数计数。诊断帧和快照是非持有的观察值，不加入根集合；其完整父视图由仍存活的父执行环境持有，实际调用快照由在途输入持有。资源不能仅因调试或快照保存而延寿。

回收点是节点结果提交、变量抹除、帧退出及调用收尾。每次先同步完成根集合变更，再求可达集合 R；当前 Run owned、且不属于 R、没有有效在途调用持有的资源进入待清理队列。borrowed 资源局部不可达时只释放持有，由所属 Run 判断最终回收。这里只扫描根的值树；locator 不包含受管依赖，无需再遍历资源间对象图。

根交接不得出现真空期：子帧退出前先登记完整返回结果，映射后同步替换为投影结果；并发分支在屏障内保留结果，异常时先排空同组任务再移除根。普通同步 builtin 求值与 env 提交之间不挂起，也无需临时根。一次扫描期间不 await，清理只在扫描完成后调度。

清理队列持有未完成责任，但不作为可重新进入业务的执行根。Child 退出前先移交成功输出的新资源，再移除自身执行根并清理其余 owned 资源；根 Run 退出时清理全部内部所有权，不向宿主导出资源。这个方案保持及时回收，避免在每个控制流分支配对增减引用。

### 5.3 在途使用与生命周期保护

框架只保护资源存续，不调度业务访问顺序。Fork-Join、并发 for_each 及 Attempt 内部协程对同一资源的使用均由业务保证正确：

* 同一调用可以在多个字段或容器元素中重复传入同一 rid，按 ID 合并为一份在途持有记录。不同调用也可以同时使用同一资源；编译器、变量补全和运行时均不增加业务访问冲突规则。
* Workflow 以在途输入根保活资源；子图、进程内子工作流及独立 Child 沿用相同生命周期规则，Child 只接收获准输入的规范描述，不附带访问模式或调用树锁。
* Worker 按 Attempt 登记 get / put 的本地使用者，保持到业务任务实际排空。待回收条目拒绝新的 get；现有使用者排空后才能析构。Workflow 超时不能解除仍在执行的本地保护。
* `builtin:*` 纯算子及 signal 只处理值或等待消息，不解引用，也不登记本地使用者；它们仍遵守执行根持有规则。

### 5.4 失联后的恢复与自动回收

输入或输出 schema 含 handle 的 Activity 禁用 native_retry，单次 `maximum_attempts=1`，由 Workflow 在处理资源结果后推进 DSL 重试。

收到可验证最终结果或带资源 details 的协作失败，表示本次业务已排空。schedule_to_start_timeout 说明任务尚未启动，可以直接执行既定路由回退；已启动调用的超时、取消或失联则先请求取消，进行有界收尾。

收尾结束或预算耗尽后，旧调用不再持有有效调度授权和执行根；Worker 本地使用记录一直保留到实际排空。未确认使用只作为诊断和本地处置责任，不把资源永久隔离或无限保活：

* 所属 Run 仍持有资源时，保留同一 rid。可恢复资源允许在后续调用中通过 locator/restore 重建实例；瞬态资源仍只能回原 Worker，实例丢失时报 execution，不能伪造恢复。
* 所属 Run 已无逻辑引用和有效在途调用持有时，自动进入回收；失联旧调用不阻止发送 dispose_resources。借入资源只由其所属 Run 作这个决定，父调用仍有效时不能因 Child 失败提前删除。
* 向旧 Worker 发出的清理请求标记本地条目待回收；实际使用者排空后执行 dispose。新调用不能取得待回收条目，已持有对象的旧任务也不能重新提交业务结果。
* persistent 外部对象仍允许在共享队列 restore 后幂等清理，不要求先等到所有失联 Worker 的排空证明。旧任务可能因对象已删除而失败；它不再具有继续访问该对象的生命周期保证。

失联不证明物理执行已停止。可恢复的可变资源须由业务、资源类及外部系统保证迟到操作安全，例如幂等、版本校验或 fencing。无法满足时使用瞬态资源或在外层创建新资源重试。框架不承诺超时后的 exactly-once 副作用。

成功输出返回后、Temporal 完成响应被持久化前仍可能崩溃或失去应答。Attempt / Child 无法判断接收者是否已接管时，不能擅自销毁可能已移交的导出资源；已知且可寻址的清理责任继续自动处理，完全未确认的资源由 Worker 退出或 TTL 兜底。这个窗口不影响已登记资源的正常自动回收。

### 5.5 被动清理

`dispose_resources` 接收 Registry 的规范描述，只处理所有者已经允许回收的资源：

* 向各登记驻留队列发送清理请求。Store 将目标置为不可业务访问的待回收状态，本地使用者排空后调用 dispose；若当前清理 Activity 的预算先耗尽，仍保留待回收标记，由最后一个本地使用者的收尾继续驱动清理，成功后摘除实例。
* dispose 失败时保留原实例及待回收状态，不能以删除表项冒充释放。后续清理点可在有限预算内重试；进程资源不靠恢复另一实例补做原对象释放。
* 非 persistent 资源只清理已有缓存；队列排队超时不证明进程已死，也不触发 restore。
* persistent 资源尚无外部删除成功确认时，可以在预算内回退创建池的共享队列，按 locator 恢复后 dispose。已确认外部删除后不再为此重建实例，但其余已登记本地副本仍需摘除。
* 返回逐项 `disposed / missing / unconfirmed` 及诊断。missing 只说明当前表中无实例；不能覆盖此前失败，也不能证明 persistent 外部对象已删除。

Store 对初始化、登记使用和清理使用同一逐 ID 同步边界；异步恢复后复查授权再交付，不允许清理后重新入表。业务方法不在该同步边界内执行，资源访问顺序仍由业务负责。成功摘除后不保留永久墓碑，待回收失败条目则仍真实承担清理责任。

被动清理受固定总预算约束，重复取消也不放弃已持有的清理任务；预算耗尽记录未确认结果。业务析构异常不改变 retry/catch 选择器、不掩盖主异常；引擎自身不变量破坏仍按引擎错误处理。Run 存续期间保留未完成责任，结束时报告无法确认的清理项。

## 六、契约生成与业务示例

### 6.1 两个 Python 执行器共用一套类型

`axcutor_runtime` 公开导出 `Handle`、`resource` 与 ContextVar 代理 `resources`。每个 Attempt 绑定独立上下文并在 finally 关闭、reset；builtin 前导注入 Handle/resources，module 按普通 import 使用。资源类定义放在挂载模块中。

含 handle 的契约才导入公共 Handle；Worker 中所有 Input/Output 与 resources 使用同一个运行期类。离线契约环境可生成仅含 rid/kind、相同相等及哈希语义的 fallback，不能参与 Worker 操作。

生成注解采用 `Handle[C]`，C 是由 kind 和 module_mounts 映射出的精确资源类。资源类导入仅置于 TYPE_CHECKING，运行期别名为 Any，不加载业务包。离线静态检查必须解析到真实类，缺失导入或退化为 Any 不算通过；生成器沿用现有私有符号防冲突规则。

输入边界从传输信封构造公共 Handle；输出只接受公共 Handle，_export 在一般 BaseModel 之前将其还原为两字段信封。Rust 和 Python 都只校验，不按输出方向改写句柄。资源来源与协议属性由资源上下文核对。

归一化 handle 类型（含 kind 与可空性）参与原有契约 SHA。`synthesize_contract_module`、`synthesize_module` 与 `generate_contracts` 均接收可选关键字参数 `module_mounts: Mapping[str, str] | None = None`；builtin 的运行期生成传入 Worker 绑定的同一映射。None 使用恒等映射，映射只影响静态 Python 导入，不改变逻辑 kind 和指纹。

`axcutor contracts` 增加与 worker 相同的可重复 `--module-mount ROOT=PYTHON_PACKAGE`。现有 `--mount ROOT=DIRECTORY` 只决定 DSL 挂载与生成文件位置，不推导 Python 包名；静态检查环境需能导入映射后的资源包，纯资源包不要求存在对应 DSL 目录。

### 6.2 事务对象自行处理业务终结

```python
# infra/postgres.py：本实例独立拥有 conn 与 tx，不包装另一受管连接。
from typing import Any
from axcutor_runtime import resource

@resource
class Tx:
    def __init__(self, conn: Any, tx: Any) -> None:
        self.conn = conn
        self.tx = tx
        self.finished = False

    async def commit(self) -> None:
        # 提交失败可能结果不明；不在同一对象上自动重试提交或回滚。
        try:
            await self.tx.commit()
        finally:
            self.finished = True
            await self.conn.close()

    async def dispose(self) -> None:
        try:
            if not self.finished:
                self.finished = True
                await self.tx.rollback()
        finally:
            # 即使回滚失败也尝试关闭；close 须容忍已关闭的连接。
            await self.conn.close()
```

```python
# 业务提交节点：框架只借用实例，业务自行处理提交结果。
from axcutor_runtime import resources
from .commit_contract import Input, Output, ReturnType

async def execute(input: Input) -> ReturnType:
    tx = await resources.get(input.tx)
    await tx.commit()
    return Output()
```

提交成功后，最后引用消失仍会调用同一个 dispose；资源类保证它不再回滚。提交失败不意味着事务可重试或副作用已撤销，外层按业务幂等约束使用新事务。

### 6.3 可恢复临时文件

```python
# infra/s3.py：上传、读取和删除的 SDK 适配方法省略。
from axcutor_runtime import resource

@resource(persistent=True)
class TempFile:
    def __init__(self, bucket: str, key: str) -> None:
        self.bucket = bucket
        self.key = key

    def locator(self) -> dict[str, str]:
        return {"bucket": self.bucket, "key": self.key}

    @classmethod
    def restore(cls, locator: dict[str, str]) -> "TempFile":
        # 只恢复定位信息，不重新创建对象；对象已删也能再次清理。
        return cls(locator["bucket"], locator["key"])

    async def dispose(self) -> None:
        # 删除不存在对象也成功；每次操作内部关闭临时本地流。
        await self.delete_object_idempotent()
```

```yaml
# etl/stage/__axcutor__.yaml：含句柄接口，只能作为执行内部的子工作流。
input:
  batch_id: {type: string}
output:
  staged_file: {type: handle, kind: infra.s3:TempFile}

graph:
  nodes:
    stage:
      input: [batch_id]
      output:
        staged_file: {type: handle, kind: infra.s3:TempFile}
      run:
        executor: module
        content: .stage
  edges:
    - start -> stage
    - stage -> end
```

```python
# etl/stage/stage.py：生成契约后，put 的结果可直接写入输出。
from uuid import uuid4
from axcutor_runtime import resources
from infra.s3 import TempFile
from .stage_contract import Input, Output, ReturnType

async def execute(input: Input) -> ReturnType:
    # 各 Attempt 使用独立代次，旧清理不得删除重试的新产物。
    file = await TempFile.upload(
        bucket="etl-scratch",
        key=f"{input.batch_id}/{uuid4().hex}.parquet",
    )
    handle = await resources.put(file)
    return Output(staged_file=handle)
```

外部临时对象必须使用唯一代次或精确对象版本，并配置存储 TTL，覆盖上传后尚未 put、未确认移交、强制终止与清理失败。TTL 是独立的存储保留策略，不能替代运行期生命周期保护，也不保证超期的业务仍能访问对象。

## 七、变更责任边界

| 模块 | 设计责任 |
| :--- | :--- |
| `ast/typespec.rs` | handle 构造器、kind 文法、子类型、字面量及运行期形状校验；复用无损 JSON |
| `compiler/{check,builtins,mod}.rs` | 私有可见性、算子边界、句柄类型检查；拒绝 signal 句柄输出，沿用现有变量补全规则 |
| `kernel/mod.rs` 及 Python/WASM 绑定 | 根接口校验与运行期信封校验；比较复用 literal_eq，不增加信封改写 |
| `resources.py`（新增） | 公共 Handle、resource、类解析、Store、Context 与本地有界清理 |
| `{builtin,module,contracts}.py` | 共用公共类型与加载器、静态类映射、导出和契约指纹 |
| `activities.py` | Attempt 使用记录、输出来源检查、成功移交、失败驻留说明、本地孤儿回收及单次收尾更新 |
| `workflow.py` | Registry、执行根扫描、路由、跨 Child 借入与移交、失联恢复及自动清理；保持透明 Child 调度 |
| `worker.py` | 复合 Worker、共享 Store、停机排空及收尾更新发送能力；沿用 RFC-0016 心跳装配 |
| `host/{service,cli}.py` | 根接口预检、contracts 的 module_mounts；维持宿主纯值接口 |
