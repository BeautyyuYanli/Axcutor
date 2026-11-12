# 05. 资源句柄（`handle`）与受管资源生命周期

在工作流编排中，数据库连接、活跃事务、内存计数器或远端受管文件等 Python 对象不能直接作为纯 JSON 值写入工作流事件历史。Axcutor 提供不透明资源句柄类型 **`type: handle`**：真实 Python 对象驻留在 Worker 进程的 `ResourceStore` 中，工作流栈帧仅传递轻量级句柄信封，并由运行时按句柄的可达性自动调度跨节点借用、亲和路由、宿主边界移交与析构回收。

::: info 底层协议参引
若需查阅运行时 `WorkflowResourceRegistry`、Activity / Child Workflow 内部交接外壳、正交所有权状态机与 Sticky 队列路由的形式化契约，请参阅 [05. 不透明资源句柄、授权与跨边界生命周期规范](../../spec/05-executor-abi/05-resource-lifecycle.md)。
:::

---

## 一、 静态声明：`__ax_resources__.yaml` 与 `type: handle`

使用受管资源分为**在资源包中声明静态头文件 `__ax_resources__.yaml`** 与**在工作流 `TypeSpec` 中引用 `type: handle`** 两步。编译器在不导入任何 Python 代码的前提下，基于挂载的资源头文件完成静态存在性、包可见性与外部边界可寻址性检查。

### 1. 资源包头文件（`__ax_resources__.yaml`）

任何包含 `__init__.py` 与 `__ax_resources__.yaml` 的目录即构成一个**资源包**（可与同目录的 `__axcutor__.yaml` 工作流包共存，也可作为纯资源包独立存在）。

```yaml
# infra/storage/__ax_resources__.yaml（对应逻辑资源包路径 infra.storage）
resources:
  # 1. 未声明公开 locator：仅限工作流执行内部传递，外部观测面隐藏内部定位符
  UploadSession:
    description: "分片上传临时会话句柄"
  # 无需附加字段时可将值设为空（null）或空映射 {}
  ScratchBuffer:

  # 2. 显式声明公开 locator：允许按闭合外部信封进出宿主根接口与 signal output
  File:
    description: "受管文件句柄"
    locator:
      type: object
      properties:
        file_id: { type: string }
```

| 头文件层级 | 语法与约束规则 |
| :--- | :--- |
| **顶层对象** | 闭合映射，**必须且只能包含 `resources` 一个键**（额外键报 `schema.unknown_field`；缺失或为空映射报 `schema.resources`）。 |
| **`resources` 映射** | **非空映射** `{ <PublicClassName>: { description?: string, locator?: TypeSpec } \| null }`；每个键必须为合法公开标识符（禁止以 `_` 开头或使用 Python 关键字，否则报 `schema.identifier`）。 |
| **资源项字段** | 仅允许可选的 `description: string` 与 `locator: TypeSpec`（未知字段如 `persistent` 报 `schema.unknown_field`；资源持久性统一由 Python `@resource` 类协议定义）。`locator` 必须为顶层非可空（`nullable == false`）且不递归包含 `handle` 的纯值 `TypeSpec`，否则报 `type.schema`。 |
| **路径限制** | 资源包点分路径首段必须等于所属挂载根名（否则报 `schema.package_path`），路径中禁止出现保留段名 `__axcutor__` 或 `__ax_resources__`；匿名入口 `"__main__"` 禁止声明资源头文件。 |

### 2. 工作流中的 `type: handle` 写法与可见性规则

在工作流的 `input`、`output` 或 `graph.variables` 中，通过 `type: handle`（或可空形式 `type: [handle, "null"]`）配合必填字段 `kind: "<ModulePath>:<PublicClassName>"` 声明句柄类型：

```yaml
# 1. 非空资源句柄与可空资源句柄
session:
  type: handle
  kind: infra.storage:UploadSession
optional_file:
  type: [handle, "null"]
  kind: infra.storage:File

# 2. graph.variables 中的初值规则：
# 非空句柄不能用字面量构造；但可空句柄可设 default: null，句柄容器可设空列表 [] 或空字典 {}
graph:
  variables:
    active_session:
      type: [handle, "null"]
      kind: infra.storage:UploadSession
      default: null
    staged_files:
      type: array
      items:
        type: handle
        kind: infra.storage:File
      default: []
```

* **名义类型匹配与容器组合**：`handle` 采用严格的名义类型匹配——仅当两个句柄的 `kind` 字符串完全相同时才满足类型等价与子类型兼容（不支持前缀或子类继承拓宽）；非空 `handle` 可单调提升为同 `kind` 的 `[handle, "null"]`，也可嵌套在 `array`、`OrderedMap` 与 `TypedDict` 中。
* **私有包可见性（`_` 前缀）**：若 `kind` 的 `<ModulePath>` 中包含以 `_` 开头的私有段（如 `billing._internal:Lease`），则仅允许其紧邻父包前缀（`billing`）子树内的工作流引用；跨包越界引用在编译期报 **`visibility.private_resource`**，引用未在任何 `__ax_resources__.yaml` 中声明的 `kind` 报 **`type.unknown_resource`**。
* **支持的内建算子**：句柄变量可通过 `builtin:assign`、`builtin:unwrap` 以及列表/映射/结构体容器算子传递和重组，相同 `kind` 的句柄可通过 `builtin:compare` 执行 `eq` / `ne` 相等性判别；句柄不支持 `.field` 字段路径投影、`builtin:math`、`builtin:logic`、`builtin:length`、`builtin:switch` 或序比较（`lt`/`lte`/`gt`/`gte`）。

### 3. 外部宿主边界与闭合句柄信封

句柄能否跨越外部宿主边界（宿主根工作流 `input` / `output` 与 `executor: signal` 的 `output`），取决于其资源类型是否在 `__ax_resources__.yaml` 中显式声明了公开 `locator`（Python 类仅实现内部 `locator` / `restore` 用于跨 Worker 恢复，不会自动对外公开）：

```yaml
# 声明公开 locator 的资源（infra.storage:File）可直接用于根 input/output 与 signal output
input:
  source_file:
    type: handle
    kind: infra.storage:File
output:
  result_file:
    type: handle
    kind: infra.storage:File
graph:
  nodes:
    await_override:
      # signal 的 input 仅声明只读依赖，仍可引用未公开 locator 的内部句柄
      input: [source_file]
      output:
        result_file:
          type: handle
          kind: infra.storage:File
      run:
        executor: signal
  edges:
    - start -> await_override
    - await_override -> end
```

对应的根提交 `input`、根完成 `result.output`、`:signal` `output` 以及运行期观测面（`frames[].variables` 与 `trace`）统一使用闭合信封表示：

```json
{
  "source_file": {
    "$handle": "infra.storage:File",
    "locator": { "file_id": "f_001" }
  }
}
```

| 边界与场景 | JSON 表示与校验规则 |
| :--- | :--- |
| **公开可寻址句柄**<br>*(根 `input` / 根 `output` / `signal` `output` / `frames` / `trace`)* | 闭合双键对象 `{"$handle": "<kind>", "locator": <value>}`：`$handle` 必须精确等于槽位声明的 `kind`，`locator` 必须通过对应 `locator` `TypeSpec` 的纯值校验；可空句柄 `[handle, "null"]` 的空值由顶层 `null` 表达。由于输入与轨迹表示同构，可直接复用 `trace` 根输入再次运行。 |
| **内部不可寻址句柄**<br>*(仅限执行内部流转；只读出现在 `frames` / `trace`)* | 在 `frames[].variables` 与 `trace` 中脱敏投影为单键对象 `{"$handle": "infra.storage:UploadSession"}`，隐藏内部 `rid` 与敏感恢复参数，且不可提交至根 `input` 或 `:signal`。若误用于 `signal` `output`，编译期报 **`type.signal_handle`**；若误用于匿名入口 `"__main__"` 的根 `input` / `output`，编译期报 **`type.root_handle`**；具名工作流包保留内部句柄接口供 `use: workflow:...` 调用，作为宿主根入口提交时由预检以 `422 input_validation_failed` 拦截。 |
| **普通业务 `object` / `map`** | 运行时仅在静态 `TypeSpec` 声明为 `handle` 的位置解析句柄信封；普通业务对象即使包含 `"$handle"` 键也按其自身 Schema 原样透传。 |

* **规范身份（Canonical `(kind, locator)`）**：对声明了公开 `locator` 的资源，单次顶层执行内以 `(kind, locator)`（按无损 JSON 字面量相等性比较）作为唯一规范身份。无论同一 `(kind, locator)` 通过根 `input`、`:signal`、Activity `resources.put(...)` 还是独立 Child Workflow 返回值多次进入工作流，均自动归并为同一运行期句柄，保证 `builtin:compare` 的 `eq` / `ne` 在进程内子调用与独立 Child Workflow 之间行为一致。
* **结构校验与访问授权边界**：编译与运行时边界校验仅验证名义 `kind` 与 `locator` 的静态值结构，不隐含跨租户或调用方访问授权。外部传入定位符的归属与权限须由宿主控制面在根提交与 `:signal` 入口预检，或由业务资源类采用带密码学签名的定位符并在 `restore(locator)` 中验签。

---

## 二、 Python 资源类定义与三类恢复协议

在 Python 资源包的 `__init__.py` 中公开导出经 **`@resource`** 或 **`@resource(persistent=True)`** 装饰的资源类（类可直接定义在 `__init__.py` 或同包目录的单文件子模块如 `_tx.py` 中并在 `__init__.py` 导出，严禁跨包重导出其他包的类；未直接添加 `@resource` 装饰器的子类不视为资源类）。

### 1. 三类受管资源生命周期模式对照

根据是否提供重建定位符（`locator` / `restore`）以及是否声明 `persistent=True`，受管资源分为以下三种模式（若资源在 `__ax_resources__.yaml` 中声明了公开 `locator` 并需从外部边界传入，其 Python 类必须定义可调用的 `locator` 与 `restore`，属于模式 2 或模式 3）：

| 资源模式 | 装饰器与钩子要求 | 跨 Worker 路由与回收语义 | 适用场景 |
| :--- | :--- | :--- | :--- |
| **1. 进程内瞬态资源**<br>*(Transient)* | `@resource`（默认 `persistent=False`）<br>• 省略 `locator`/`restore`，或 `locator(self)` 返回 `None`<br>• 可选 `dispose(self)` | • **固定原 Worker 路由**：后续消费该句柄的 Activity 必须路由回创建它的同一 Worker 进程内存；若原 Worker 失联或实例丢失则报 `execution` 错误。<br>• **仅析构本地实例**：回收时仅在驻留 Worker 上调用 `dispose()`。 | 进程内内存游标、不可重建的活跃数据库事务（`Tx`）、本地打开的流或临时内存缓冲。 |
| **2. 可恢复非持久资源**<br>*(Restorable Non-Persistent)* | `@resource`（默认 `persistent=False`）<br>• 成对实现 `locator(self) -> JSONValue` 与 `@classmethod/@staticmethod restore(locator) -> Self`<br>• 可选 `dispose(self)` | • **Sticky 优先 + 跨 Worker 自动重建**：优先路由至已缓存该实例的 Worker；若缓存未命中或 Sticky 排队超时（5 秒），可在同池任意 Worker 上调用 `restore(locator)` 重建等价客户端。<br>• **不为析构新建连接**：回收时仅析构已驻留的进程内副本。 | 指向外部服务或只读快照的无状态/可重连客户端包装、带连接参数的懒加载会话。 |
| **3. 可恢复持久资源**<br>*(Persistent)* | `@resource(persistent=True)`<br>• **必须**同时实现返回非 `None` 的 `locator(self)`、`restore(locator)` 与幂等 `dispose(self)` | • **跨 Worker 重建与兜底幂等删除**：代表独立于单个 Worker 存活的外部实体；当归工作流所有且所有原驻留副本不可达时，运行时可在目标池共享队列通过 `restore(locator)` 恢复定位包装并执行 `dispose()` 删除外部实体。<br>• **Worker 停机与宿主所有权保护**：Worker 停机或资源归宿主持有时仅摘除本地内存引用，不误删外部持久实体。 | 远端对象存储文件（S3 Object）、外部预占锁租约、待清理或待移交宿主的持久产物。 |

### 2. 钩子签名与幂等设计规范

* **同步与异步兼容**：`locator(self)`、`restore(cls, locator)` 与 `dispose(self)` 均支持编写为普通同步方法或 `async def` 异步方法。
* **定位符无损 JSON 约束**：`locator(self)` 返回的值会经无损 JSON 编解码深拷贝保存（其中超长整数或小数在 `restore(locator)` 接收时可能表示为大整数或 `Decimal`，传入第三方 SDK 前由资源类按需转换）；Worker 首次通过 `restore(locator)` 恢复实例后，会校验 `instance.locator()` 与输入定位符精确相等。
* **持久资源 `restore` 与 `dispose` 的幂等契约**：
  * 对于 `@resource(persistent=True)`，`restore(cls, locator)` **只应构造保存定位信息的轻量级包装实例**，不应在 `restore` 内部检查外部实体是否存在或重新创建外部实体；
  * 这样即使外部实体已被前一次重试删除，`restore` 仍能成功返回包装实例并交由 `dispose(self)` 以“不存在亦视为成功”的幂等语义完成清理确认（若 `restore` 因外部实体缺失而抛错，运行时只能将本次清理记为 `unconfirmed`）：

```python
# infra/storage/__init__.py：可恢复持久资源示例（@resource(persistent=True)）
from typing import Any
from axcutor_runtime import resource


@resource(persistent=True)
class StagedBlob:
    def __init__(self, bucket: str, key: str, version_id: str) -> None:
        self.bucket = bucket
        self.key = key
        self.version_id = version_id

    def locator(self) -> dict[str, str]:
        # 返回纯 JSON 可序列化的精确版本定位符
        return {"bucket": self.bucket, "key": self.key, "version_id": self.version_id}

    @classmethod
    def restore(cls, locator: dict[str, Any]) -> "StagedBlob":
        # 仅恢复定位包装，不在此处发起网络请求或重建底层对象
        return cls(
            bucket=str(locator["bucket"]),
            key=str(locator["key"]),
            version_id=str(locator["version_id"]),
        )

    async def dispose(self) -> None:
        # 按精确 version_id 幂等删除外部临时对象；若对象已不存在同样正常返回
        ...
```

---

## 三、 在执行器中注册与借用资源（`resources.put` / `resources.get`）

在 `executor: module` 或 `executor: builtin` 的 `async def execute(input: Input) -> ReturnType:` 函数体内，通过 `axcutor_runtime.resources` 提供的两个异步方法操作资源（`builtin` 执行器已默认注入 `resources` 与 `Handle`）：

| API 方法 / 类型 | 签名 | 行为与约束 |
| :--- | :--- | :--- |
| **`resources.put`** | `async def put(instance: T) -> Handle[T]` | 校验 `instance` 为合法的 `@resource` 类实例并提取其 `locator`，在当前 Worker 的 `ResourceStore` 中注册唯一 `rid`，返回类型化的不可变 `Handle[T]`（具有只读属性 `handle.rid` 与 `handle.kind`）。同一 Python 对象实例严禁重复 `put`。 |
| **`resources.get`** | `async def get(handle: Handle[T]) -> T` | 核对 `handle` 是否属于当前节点 `input` 获准传入的句柄或本次尝试新 `put` 的句柄；若本地未命中且资源支持 `restore`，自动调用 `restore(locator)` 恢复并缓存实例，返回精确类型 `T`。 |

```python
# 1. 资源包定义：infra/counter/__init__.py
from axcutor_runtime import resource


@resource
class Counter:
    def __init__(self, initial: int) -> None:
        self.value = initial

    async def dispose(self) -> None:
        self.value = 0
```

```python
# 2. 创建并导出句柄的节点实现（output.counter 声明为 { type: handle, kind: infra.counter:Counter }）
from axcutor_runtime import resources
from infra.counter import Counter
from .open_counter_contract import Input, Output, ReturnType


async def execute(input: Input) -> ReturnType:
    # put() 接管实例并返回 Handle[Counter]，放入 Output 导出至工作流变量表
    handle = await resources.put(Counter(input.initial))
    return Output(counter=handle)
```

```python
# 3. 借用句柄的消费节点实现（input.counter 注解自动生成为 Handle[Counter]）
from axcutor_runtime import resources
from .step_counter_contract import Input, Output, ReturnType


async def execute(input: Input) -> ReturnType:
    # get() 根据 Handle[Counter] 借出进程内的 Counter 实例
    counter = await resources.get(input.counter)
    counter.value += input.delta
    return Output(current_value=counter.value)
```

* **借用作用域边界**：`resources.get(handle)` 借出的 Python 实例仅限在当前 Activity 尝试内部使用，严禁将其存入模块全局变量或交由脱离当前尝试生命周期的后台游离任务继续访问；在资源的 `locator`、`restore`、`dispose` 钩子内部严禁反向调用 `resources.put` 或 `resources.get`。
* **并发借用与业务同步职责**：运行时负责保障资源在工作流持有句柄期间不被提前析构，但不会对多个并发分支同时借用同一句柄的方法调用强加串行锁。若底层对象（如单个数据库连接或事务）不支持并发协程复用，应由工作流拓扑按顺序编排或由资源类自身维护内部互斥。

---

## 四、 所有权移交、自动回收与超时边界

1. **Activity 出口校验与局部孤儿即时回收**：
   * 在 `put(instance)` 完成注册前，实例的异常清理责任属于业务代码；`put(instance)` 成功后，实例由当前尝试的 `AttemptResourceContext` 接管；
   * 仅当 Activity 正常返回且所选分支的 `output` 完整通过契约校验（对于声明了公开 `locator` 的新导出资源，Worker 出口与 Workflow 接收边界还会双重校验 `instance.locator()` 非空且符合声明的 `locator` `TypeSpec`，违约映射为节点 `contract` 异常）时，**实际出现在最终有效输出字段中**的新句柄才会移交工作流 `WorkflowResourceRegistry` 长期持有；
   * 若某次 `put` 创建的句柄未被放入最终返回的分支 `output`（节点内临时资源），或者当前尝试因业务异常、契约违约、超时取消而失败，则本次尝试新创建的所有未移交资源均留在当前 Attempt 本地作为孤儿清理（若孤儿定位符与存活句柄或宿主资源指向同一持久实体，自动跳过底层实体删除以免误伤存活资源；若多个未导出孤儿指向同一新建持久实体且无存活别名，仅保留单一删除者执行幂等清理）。
2. **工作流可达根扫描与覆写/退帧自动回收**：
   * 工作流在每次节点提交、变量被新值覆写、条件分支反定值抹除（如 `builtin:unwrap` 走 `.none`）以及子图/子工作流栈帧退出时，自动扫描当前所有活跃执行帧、待提交完整结果与在途调用输入中的可达句柄；
   * 一旦某个归当前工作流所有的句柄不再被任何可达根引用（或子工作流返回的句柄字段未在父节点的 `with.output` 中接收），运行时立即将其标记为待回收并向驻留 Worker 派发 `dispose_resources` 清理任务；
   * 跨独立 Child Workflow 调用时，父工作流传入的输入句柄作为只读借用（`borrowed`）传递，子工作流退出或失败时不会销毁父工作流借出的资源；子工作流新创建并通过正常输出返回的句柄则将所有权移交父工作流管理。
3. **宿主边界所有权与根出口移交**：
   * **宿主传入资源**：经根工作流 `input` 或 `:signal` `output` 传入的公开资源，其外部持久实体删除权自始归属外部宿主；工作流在句柄不可达或结束退出时仅驱逐 Worker 内存缓存并关闭非持久本地连接，绝不删除底层持久实体；
   * **内部创建资源的根出口移交**：工作流内部通过 `resources.put(...)` 新建的资源在运行期间归工作流所有；仅当根工作流**正常成功完成**且该句柄出现在最终根 `output` 中时，其外部持久实体所有权才在收尾时单向移交宿主（若根工作流失败或被取消，不发生移交，内部新建持久资源照常回收）；
   * **观测与重选不转移删除权**：内部新建的公开资源句柄在运行期间经 `frames` / `trace` 被观测到、或通过 `:signal` 重新选回同一执行时，仍保持工作流内部所有权，直至根 `output` 成功提交才移交宿主；若 `:signal` 提交的定位符正处于内部资源的待删除阶段，请求以 `422 signal_validation_failed` 拒绝，节点保持挂起等待。
4. **超时排空、重试与外部副作用边界**：
   * 包含 `handle` 输入或输出的 Activity 节点统一由工作流状态机驱动重试（自动禁用后端透明原生重试），以便在每次重试前重新核对资源驻留状态并清理上一次失败尝试的孤儿资源；
   * 当调用超时或取消时，工作流等待取消排空与本轮已知资源清理共享 **30 秒**收尾预算；若本地业务任务在 30 秒后仍未退出，Worker 的 `ResourceStore` 会继续保活其正在借用的本地对象直至任务实际排空后再执行 `dispose()`；
   * Activity 在业务函数返回并完成契约校验时即按本地 `timeout_ms` 预算锁定业务结果，随后的本地孤儿清理使用独立预算，不会将已按时完成的业务成功改判为本地超时（但若清理期间触及后端的 `start_to_close_timeout` 硬期限，工作流仍会按后端 `timeout` 处理）。

---

## 五、 契约桩生成与 Worker 模块映射（`--module-mount`）

当 `executor: module` 节点的输入或输出包含 `handle`（如 `kind: infra.postgres:Tx`）时，`axcutor contracts` 会在生成的 `*_contract.py` 契约桩中自动生成精确资源类的静态类型注解 `Handle[Tx]`（并在 `if TYPE_CHECKING:` 分支下导入 `from infra.postgres import Tx`）：

```sh
# 1. 默认映射（逻辑根名与已安装 Python 顶层包名一致）：
#    --mount 挂载工作流包与资源包目录（纯资源包 infra 也须通过 --mount 挂载以提供 __ax_resources__.yaml）
uv run --locked axcutor contracts workflows/billing \
  --mount billing=workflows/billing \
  --mount infra=workflows/infra

# 2. 自定义 Python 包前缀映射（--module-mount ROOT=PYTHON_PACKAGE）：
#    当物理 Python 包安装在 company.billing 与 company.infrastructure 下时：
uv run --locked axcutor contracts workflows/billing \
  --mount billing=workflows/billing \
  --mount infra=workflows/infra \
  --module-mount billing=company.billing \
  --module-mount infra=company.infrastructure
```

* **`--mount` 与 `--module-mount` 职责正交**：
  * `--mount ROOT=DIR` 用于编译期扫描目录下的 `__axcutor__.yaml` 与 `__ax_resources__.yaml`，并确定 `*_contract.py` 契约桩文件的写入位置；
  * `--module-mount ROOT=PYTHON_PACKAGE` 告诉契约生成器与运行时 Worker 如何将逻辑根名（如 `infra`）映射为可导入的 Python 包路径（如 `company.infrastructure`）。该物理前缀映射不改变工作流中的逻辑 `kind`（仍为 `infra.postgres:Tx`），也不影响契约的 `__axcutor_contract_sha256__` 指纹。
* **Worker 启动一致性**：启动 Worker 时使用相同的 `--module-mount` 参数（如 `axcutor worker --module-mount billing=company.billing --module-mount infra=company.infrastructure`）；Worker 在运行期仅按映射导入已安装的 Python 包并校验 `@resource` 类协议，无需读取磁盘上的 `__ax_resources__.yaml`。
