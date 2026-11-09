# RFC-0013: 不透明句柄类型（`handle: module:name`）、`@resource` Python 资源类协议（`locator` / `restore` / `dispose`）、确定性引用计数逻辑堆与 Sticky Worker 物理资源存储规范

* **状态**：Proposed
* **所属分类**：类型系统扩展、静态并发流分析、运行时内存模型与跨 Activity 资源生命周期管理（Type System Extension, Static Concurrency Analysis, Runtime Heap & Resource Lifecycle）
* **前置关联提案**：
  * [`RFC-0009: 静态结构化子类型偏序（<:）与容器协变规范`](./0009-static-subtyping-and-covariance.md)
  * [`RFC-0011: Axcutor 编译器前后端与 Temporal 运行时参考实现架构`](./0011-compiler-and-temporal-runtime-architecture.md)
  * [`RFC-0012: 结构化 run 执行器分派机制、移除自然语言变量正则提取与 builtin 可信 Python 执行器规范`](./0012-run-executor-and-builtin-python.md)
  * [`RFC-0014: 基于预置 Python 模块引用的 module 执行器、__axcutor__.yaml 包同构布局与契约模板 SHA 校验规范`](./0014-module-python-executor.md)
  * [`RFC-0015: 统一工作流包（Package）寻址、显式根包挂载与私有可见性规范`](./0015-unified-package-and-import-system.md)
* **核心解决的问题**：
  1. **在不破坏纯值语义与闭合类型系统的前提下统一表达三类外部资源实体**：将 **纯内存瞬态资源**（如数据库事务 `infra.postgres:Tx`）、**可恢复进程缓存资源**（如数据库连接 `infra.postgres:Conn`、有状态会话 `infra.agent:Session`）与 **可恢复外部持久化临时资源**（如 S3 临时文件 `infra.s3:TempFile`）统一建模为第 7 类基础类型构造器 **`type: handle`**（携带指向 Python `@resource` 类的 `kind: <ModulePath>:<ClassName>` 与可选独占修饰符 `exclusive?: boolean`），避免发明 `graph.resources` 或 `graph.sessions` 等平行于 `graph.variables` 的冗余图内变量表；
  2. **通过 `kind: <ModulePath>:<ClassName>` 自寻址与 `@resource` Python 类三要素协议（`locator(self)` + `restore(cls, locator)` + `dispose(self)`）将三类资源 100% 统一内聚于 Python 侧**：
     * 取消 `create_worker` 资源注册表与 YAML 顶层配置块：所有资源（含瞬态资源与可恢复资源）统一在挂载的 Python 模块中通过 `@resource(persistent=...)` 装饰资源类；业务代码只需构造出 `@resource` 类实例并调用 **`await resources.put(instance, exclusive=...)`**，运行时即自动从实例所属类推导 `kind = "<ModulePath>:<ClassName>"`、自动调用 `instance.locator()` 提取可序列化定位符并绑定 `instance.dispose()`；
     * `handle` 的 `kind` 中 `.` 仅表示 [`RFC-0015`](./0015-unified-package-and-import-system.md) 的逻辑模块包路径层级，`:` 后为 `@resource` 类名，不再定义基于点分前缀的 `kind` 子类型关系（仅要求 `T_1.kind == T_2.kind` 精确匹配）；不同工作流只需在 YAML 中声明相同的 `kind: <ModulePath>:<ClassName>` 并在 Python 中 `import` 该类即可自然共享；
     * 对不定义 `locator` / `restore`（或 `locator()` 返回 `None`）的 **纯内存瞬态资源（数据库事务 `infra.postgres:Tx`）**：强绑定创建它的 Sticky Worker 内存，支持通过 `resources.discard(h)` 在手工提交事务后跳过回滚析构；Worker 崩溃时内存丢失即事务丢失，节点快速失败（Fail-fast）交由外层事务子图重试；
     * 对定义 `locator` + `restore` 且 `persistent: false` 的 **可恢复进程缓存资源（数据库连接 `infra.postgres:Conn`）**：原 Worker 存活时优先 Sticky 路由复用热连接；原 Worker 崩溃或跨 Worker 汇聚时，在任意存活 Worker 上按 `kind` 加载 `@resource` 类并通过 `cls.restore(locator)` 透明恢复连接继续业务；引用归零时仅在仍缓存该连接的存活 Worker 上执行 `value.dispose()`；
     * 对定义 `locator` + `restore` 且 `persistent: true` 的 **可恢复外部持久化临时资源（S3 临时文件 `infra.s3:TempFile`）**：支持跨 Worker 恢复、多驻留副本各自执行幂等 `value.dispose()`（释放本地流并删除外部对象）与共享队列保底清理；清理失败记录诊断，未完成移交或清理的外部对象由存储 TTL 兜底，不承诺外部服务故障下绝对无泄漏；
  3. **编译期静态并发互斥分析 + Workflow 按资源 ID 借用检查 + Worker 本地 Attempt 防护**：保留独占性单调子类型规则与静态词法检查；运行期在调度前按实际 `rid` 检查别名、重复元素和跨 Worker 的并发借用，并在 Worker 的 `get`、`close`、`discard` 中统一检查本地借用状态；
  4. **建立“栈帧 `Handle` 信封 + 运行时全局 `Registry` 逻辑堆 + Worker 进程内 `ResourceStore` 物理表”的分层架构**：在 Temporal 确定性 Workflow 沙箱中维护零外部 DB 依赖的引用计数逻辑堆（`self._registry`），在 Worker 进程内维护持有真实 `@resource` 实例、按 `kind` 按需加载 `@resource` 类的无墓碑 `ResourceStore`；
  5. **实现双层防泄漏回收闭环（Attempt 级孤儿清理 + 栈帧引用计数归零 `dispose_resources`）、悬垂导出契约拦截与显式/被动析构解耦协议，并与 `builtin`（RFC-0012）及 `module`（RFC-0014/0015）执行器深度对齐**：严格维持被动回收 `gc_dispose` 作为编程语言级无异常（`noexcept`）析构函数的定位，同时通过 `ContextVar` 注入与契约模板生成让 `builtin` 和 `module` 执行器共享完全一致的 `resources` 与 `Handle` 编程体验。

---

## 一、 背景与设计动机

### 1.1 闭合纯值类型系统面对外部实体时的三类典型资源与 `@resource` 特征矩阵

在现有规范中，`TypeSpec` 仅包含 6 种内联 JSON 值类型（`string`, `integer`, `number`, `boolean`, `array`, `object`）。然而在实际业务工作流中，节点之间往往需要传递无法或不适合直接内联为普通 JSON 数据的外部资源实体。按 **跨进程可恢复性（Recoverability）** 与 **持久化副作用范围（Persistence Scope）** 划分，外部资源可分为三类典型形态，且全部统一由 `@resource` 类三要素（`locator`、`restore`、`dispose`）表达：

| 维度 | 1. 数据库事务 (`infra.postgres:Tx`) | 2. 数据库连接 / 会话 (`infra.postgres:Conn`) | 3. S3 上的临时文件 (`infra.s3:TempFile`) | *(附) S3 持久文件 (`infra.s3:File`)* |
| :--- | :--- | :--- | :--- | :--- |
| **本质特征** | **纯内存瞬态绑定（Volatile / Ephemeral）** | **可恢复 + 进程内缓存（Reconstructible + Process-Cached）** | **可恢复 + 外部持久化清理（Reconstructible + Persistent External）** | **可恢复 + 无外部删除副作用** |
| **定位符提取 `self.locator()`** | **未定义**（或返回 `None`；状态绑定在特定 Worker 内存与未提交的 TCP 事务上下文中） | **返回 JSON 值**（如 `{"dsn": self.dsn}` 或 Session ID） | **返回 JSON 值**（如 `{"bucket": self.bucket, "key": self.key}`） | **返回 JSON 值**（同左） |
| **跨 Worker 恢复 `cls.restore(locator)`** | **未定义**（不可在其他 Worker 上恢复） | **定义 `@classmethod`**（根据 `locator` 建立并返回新 `Conn` 实例） | **定义 `@classmethod`**（根据 `locator` 重新打开并返回 `TempFile` 实例） | **定义 `@classmethod`**（同左） |
| **单一析构方法 `self.dispose()`** | `await self.tx.rollback()`；`await self.conn.close()` | `await self.conn.close()` | `await self.delete_object()` 并关闭本地流 | 无或仅关闭本地流 |
| **原 Worker 存活时的调度与销毁** | 强制路由回原 Worker 复用 `Tx` 实例；异常销毁时在原 Worker 上调 `value.dispose()`，正常提交后通过 `discard` 注销 | **优先 Sticky 路由回原 Worker** 复用内存热连接；销毁时在缓存了该连接的存活 Worker 上调 `value.dispose()` | 优先路由回原 Worker；销毁时在所有缓存副本所在 Worker 上调幂等 `value.dispose()` 删除 S3 对象并释放本地流 | 任意或优先原 Worker |
| **原 Worker 崩溃 / 跨 Worker 汇聚时的业务连续性** | **不可恢复**：随 Worker 崩溃内存丢失即彻底丢失，节点必须快速失败（Fail-fast），交由外层包裹整个事务的子图重试 | **透明恢复继续业务**：后续节点回退至存活的新 Worker，在 `await resources.get(h)` 时按 `kind` 加载 `Conn` 类并通过 `Conn.restore(locator)` 自动恢复连接并缓存，业务无感继续 | **透明恢复继续业务**：任意存活 Worker 在 `await resources.get(h)` 时按 `kind` 加载 `TempFile` 类并通过 `TempFile.restore(locator)` 恢复访问对象，业务无感继续 | **透明恢复继续业务** |
| **原 Worker 崩溃时的被动析构（`gc_dispose`）** | 原 Worker 已死，OS 已断开 TCP 连接、DB 已自动回滚事务，**无需在其他 Worker 执行** | 原 Worker 已死，其内存连接已随进程消亡断开；若无其他存活 Worker 缓存该连接，**绝不** 为了关连接而在新 Worker 上专门 `restore` 新建连接再关掉（`persistent: false`） | 原 Worker 虽死，但 **S3 上的临时文件仍在**！自动回退至共享队列 `base_queue`，由任意存活 Worker **按 `kind` 加载 `TempFile` 类，先调 `TempFile.restore(locator)` 恢复实例，再调 `value.dispose()`** 完成清理（`persistent: true`） | 无需外部删除（`persistent: false`） |

### 1.2 为什么统一收敛为单一的 `type: handle`、`kind: <ModulePath>:<ClassName>` 与 `@resource` 三要素协议？

1. **YAML 类型系统层面的高度同构（单一 `type: handle`）与 Python 模块包模型（RFC-0015）无缝对齐**：
   * **栈帧内表示同构**：无论底层资源是数据库事务、数据库连接还是 S3 临时文件，在控制流栈帧 $\rho$ 中均表现为指向带外实体的轻量级不透明引用信封（Opaque Handle Envelope）；
   * **`kind: <ModulePath>:<ClassName>` 直接统一类型标识与 Python `@resource` 类寻址**：`.` 严格代表 [`RFC-0015`](./0015-unified-package-and-import-system.md) 中的 Python 逻辑模块路径（如 `infra.s3`），`:` 后代表模块内由 `@resource` 装饰的资源类名（如 `TempFile`）。这样既消除了“YAML 资源配置块与 Python 业务代码割裂”的问题，又让任意多个平行工作流（无需存在 `use: workflow:...` 调用关系）只需在 YAML 中声明相同的 `kind: infra.s3:TempFile`、在 Python 中 `from infra.s3 import TempFile` 即可天然共享同一套资源定义；
   * **取消 `kind` 前缀子类型**：既然 `.` 表示包/模块层级而非类型继承层级，两个 `handle` 类型兼容当且仅当它们的 `kind` 字符串完全相等（$T_1.\text{kind} = T_2.\text{kind}$），子类型关系仅体现在可空性（`Nullable`）与独占性修饰符（`exclusive`）的单调提升上。

2. **首次创建、跨 Worker 恢复（`restore`）与析构（`dispose`）职责正交且完全内聚于 `@resource` 类**：
   * **消除 `factory` 与 `create` 的语义歧义**：
     * 资源类的普通构造/业务创建方法（如 `__init__`、`open`、`upload`、`begin`）保持纯正的 Python 语义，负责在业务节点中首次产生 `@resource` 实例；
     * **`locator(self)`** 负责从实例提取可跨 Worker 序列化的定位符；
     * **`restore(cls, locator)`** 专职负责当句柄漂移到新 Worker（或共享队列保底清理）时，根据 `locator` 恢复出 Python 实例；
     * **`dispose(self)`** 专职负责在引用计数归零或显式 `close` 时释放本地流与清理外部对象；
     * **`await resources.put(instance, exclusive=...)`** 只需接收构造好的 `@resource` 实例本身，由框架自动读取其类对应的 `kind` 与 `instance.locator()` 完成注册，无需调用方手写字符串 `kind` 或重复拼装 `locator` 字典。
   * **单一 `dispose(self)` 无异常析构方法**：
     * 资源在仍被引用期间（`ref_count > 0`）不做自动提前缓存淘汰；当引用计数归零（或显式调用 `close`）且在途借用结束后，所有登记了驻留责任的存活 Worker 副本均执行同一个幂等的 `value.dispose()`（在 `finally` 中释放各自持有的本地流/缓存，并对共享外部对象执行幂等删除）；
     * 当所有驻留副本均因 Worker 崩溃失联而未能确认完成外部清理时，存活的新 Worker 只需 **按 `kind: <ModulePath>:<ClassName>` 导入该 `@resource` 类，先调用 `cls.restore(locator)` 恢复出 `value` 实例，再调用 `value.dispose()`**，即可完成 S3 临时文件的保底删除；
     * 同时，所有由引用计数归零或孤儿回收被动触发的 `dispose`，均严格保持编程语言中 **不可向控制流抛错（`noexcept`）的析构函数** 定位（等同于 Rust `Drop`、C++ `~T() noexcept`、Python `__del__`），绝不破坏 `builtin:*` 纯算子零异常不变量，也绝不在异常栈展开期间掩盖原始业务主异常。

---

## 二、 类型系统、`kind: <ModulePath>:<ClassName>` 寻址文法与静态并发检查规范（`axcutor-core`）

### 2.1 `TypeSpec` 良构性公理扩展

在基础非空类型集合 $\mathcal{B}$ 中新增 `"handle"`：

$$
\mathcal{B} = \{\texttt{"string"}, \texttt{"integer"}, \texttt{"number"}, \texttt{"boolean"}, \texttt{"array"}, \texttt{"object"}, \mathbf{\texttt{"handle"}}\}
$$

当基础类型 $b = \texttt{"handle"}$（含 `type: "handle"` 或显式可空 `type: ["handle", "null"]`）时，必须满足以下构造器约束：

1. **必填字段 `kind: ResourceKindSpec`（`<ModulePath>:<ClassName>` 格式）**：
   * `kind` 必须为由单个冒号 `:` 分隔的 **`<ModulePath>:<ClassName>`** 字符串：
     ```txt
     ResourceKindSpec ::= PackagePath ":" PackageSegment ;
     ```
     * 冒号左侧 `<ModulePath>` 必须满足 [`RFC-0015`](./0015-unified-package-and-import-system.md) 的 `ast::package_path` 规则（非空点分路径，首段不以 `_` 开头，每段为合法 ASCII 标识符且排除 Python 35 个硬关键字与 `"__axcutor__"`，末段不为 `"yaml" | "yml" | "json"`，例如 `"infra.postgres"`、`"infra.s3.temp"`、`"billing._internal"`）；
     * 冒号右侧 `<ClassName>` 必须满足 `ast::package_segment`（合法 ASCII 标识符且排除 Python 硬关键字，例如 `"Tx"`、`"Conn"`、`"TempFile"`）；
     * 在 `TypeSpec::parse` 与运行期信封校验中，统一按 `kind.split_once(':').is_some_and(|(m, n)| ast::package_path(m) && ast::package_segment(n))` 校验。
   * **复用 [`RFC-0015`](./0015-unified-package-and-import-system.md) 的 `_` 私有可见性检查**：
     * 若 `<ModulePath>` 中包含以 `_` 开头的私有段（或 `<ClassName>` 以 `_` 开头），则该 `kind` 受与 `ast::workflow_visible(caller_workflow_id, module_path)` 相同的包级私有可见性保护（匿名工作流 `"__main__"` 或父包子树外的其他工作流包引用私有 `kind` 时，编译期报 `visibility.private_resource` 错误）。
2. **可选字段 `exclusive?: Boolean`（默认 `false`）**：
   * 当 `exclusive: true` 时，表示该句柄指向 **不可并发重入的独占状态资源**（如数据库事务 `infra.postgres:Tx`、单连接会话 `infra.postgres:Conn`、有状态 `infra.agent:Session`）；
   * 当 `exclusive: false` 或省略时，表示该句柄指向 **可安全并发共享读取的资源**（如 S3 临时文件 `infra.s3:TempFile`、不可变文件 `infra.fs:Image`、线程安全连接池）；
   * 在 AST 规范化阶段（`TypeSpec::to_value`），唯一规范形态为：**当且仅当 `exclusive == true` 时显式输出 `"exclusive": true`，当 `exclusive == false` 时一律省略 `"exclusive"` 字段**。
3. **可选字段 `description?: String`**；
4. **禁止字段**：禁止出现 `enum`、`items`、`properties`、`required`、`additionalProperties`。
5. **容器自由组合、算子支持边界与禁止字段投影**：
   * `handle` 类型允许作为 `array.items`、`TypedDict.properties.<f>` 或 `OrderedMap.additionalProperties` 的元素/字段类型，也允许被 `builtin:unwrap`、`builtin:assign`、`builtin:array_append`、`builtin:map_set` 等多态算子透明流转；
   * **允许等值比较（`builtin:compare` 的 `eq` / `ne`）**：`handle`（及含 `handle` 的容器）允许参与 `builtin:compare (op: eq | ne)`，用于判断可空句柄是否为 `null` 或判断两个相同 `kind` 的句柄变量是否指向同一底层资源 ID（`"$handle"`）；
   * **禁止字段投影与标量/有序算子**：`handle` 为原子不透明类型，禁止对其执行 `.field` 静态字段路径投影，禁止传入 `builtin:math`、`builtin:logic`、`builtin:switch` 以及 `builtin:compare` 的有序比较（`lt`/`lte`/`gt`/`gte`）。

```yaml
# 1. 独占瞬态资源句柄（绑定 infra/postgres.py 中的 @resource class Tx，禁止并发共享读取）
db_tx:
  type: handle
  kind: infra.postgres:Tx
  exclusive: true

# 2. 独占可恢复资源句柄（绑定 infra/postgres.py 中的 @resource class Conn）
db_conn:
  type: handle
  kind: infra.postgres:Conn
  exclusive: true

# 3. 共享可恢复资源句柄（绑定 infra/s3.py 中的 @resource(persistent=True) class TempFile）
temp_dataset:
  type: handle
  kind: infra.s3:TempFile
```

### 2.2 名义等价与子类型偏序（$T_1 <: T_2$）：精确匹配 `kind`

在 `crates/axcutor-core/src/ast/typespec.rs` 中扩展 `TypeKind::Handle { kind: String, exclusive: bool }`。由于 `kind` 中的 `.` 表示模块包路径而非子类型继承层级，**不对 `kind` 做任何前缀子类型比较**：

1. **静态等价**（$T_1 \equiv T_2$）：
   当 $T_1, T_2$ 均为 `handle` 时：

   $$
   T_1 \equiv T_2 \iff \text{Nullable}(T_1) = \text{Nullable}(T_2) \;\land\; T_1.\text{kind} = T_2.\text{kind} \;\land\; T_1.\text{exclusive} = T_2.\text{exclusive}
   $$

2. **静态子类型偏序**（$T_1 <: T_2$）：
   当 $T_1, T_2$ 均为 `handle` 且满足可空性单调提升公理（$\text{Nullable}(T_1) \implies \text{Nullable}(T_2)$）时，$T_1 <: T_2$ 当且仅当同时满足以下两条公理：
   * **`kind` 精确相等**：

     $$
     T_1.\text{kind} = T_2.\text{kind}
     $$

   * **独占性单调提升（Exclusivity Monotonicity，非独占兼容于独占）**：

     $$
     T_1.\text{exclusive} \implies T_2.\text{exclusive}
     $$

     （即：同一 `kind` 的非独占句柄 `exclusive: false` 可安全作为独占句柄 `exclusive: true` 的子类型传入受限串行上下文使用；但独占句柄 `exclusive: true` **严禁** 赋给非独占目标类型 `exclusive: false`，防止独占句柄被拓宽后逃逸进入并发分支。同时由 $(\text{Exclusive}(T_1) \implies \text{Exclusive}(T_2)) \land (\text{Exclusive}(T_2) \implies \text{Exclusive}(T_1)) \iff \text{Exclusive}(T_1) = \text{Exclusive}(T_2)$，偏序集反对称性 $T_1 \equiv T_2 \iff T_1 <: T_2 \land T_2 <: T_1$ 严格保持。）

### 2.3 独占资源句柄（`exclusive: true`）的静态结构化并发互斥分析

为防止同一独占资源（如单个数据库事务 `tx`、单连接 `conn` 或单个 `agent.session`）被多个并发协程同时操作导致底层协议错乱，编译器在静态分析阶段定义递归谓词 $\text{HasExclusive}(T)$：

$$
\text{HasExclusive}(T) = \begin{cases}
T.\text{exclusive} & \text{if } T \text{ is } \texttt{Handle} \\
\text{HasExclusive}(T.\text{items}) & \text{if } T \text{ is } \texttt{Array} \\
\text{HasExclusive}(T.\text{additionalProperties}) & \text{if } T \text{ is } \texttt{OrderedMap} \\
\exists f \in \text{dom}(T.\text{properties}),\; \text{HasExclusive}(T.\text{properties}[f]) & \text{if } T \text{ is } \texttt{TypedDict} \\
\texttt{false} & \text{otherwise}
\end{cases}
$$

编译器对 Axcutor 的两类结构化并发原语强制执行以下静态借用检查（注：静态检查针对词法外层变量共享；容器重复元素和变量别名由第 3.3 节的 Workflow 资源 ID 借用检查及第 3.4.1 节的 Worker 本地防护共同处理）。下文 `Injected(N)` 为该节点实际配置的迭代注入变量名集合，包括 `for_each.item` 及非空的 `key`、`index`，不硬编码为字面名称 `item`、`key`、`index`：

1. **禁止并发 `for_each`（`concurrency > 1`）捕获外层独占资源（但允许并发遍历元素本身为独占句柄的集合）**：
   * 当节点 $N$ 挂载了 `for_each` 且 **`for_each.concurrency > 1`** 时（注：`concurrency == 1` 为串行顺序迭代，允许安全使用外层独占资源）：
   * **允许 `item` 本身为独占句柄**：允许 `for_each.source` 的元素类型为 `exclusive: true` 的句柄（例如并发遍历由多个不同独立 `infra.postgres:Conn` 或 `infra.agent:Session` 组成的数组，每个迭代协程通过 `item` 独占操作各自的资源；若数组中含重复的同一句柄，由运行期独占借用锁拦截）；
   * **严禁节点 `body` 捕获任何含独占句柄的外层变量**：直接检查 **节点 `body` 自身声明的所有外层读取引用**（严禁仅因为某变量 $v$ 充当了 `for_each.source` 的根变量 $\text{RootVar}(N.\text{for\_each.source})$ 就豁免节点 `body` 对 $v$ 自身或其实体字段的直接读取；例如当 `source: ctx.items` 时，若节点 `body` 的 `input` 同时直接捕获了含独占句柄的 `ctx`，必须拦截）：
     * 对 `NodeBody::Run` 与 `NodeBody::Graph`：对任意 $v \in N.\text{body.input} \setminus \text{Injected}(N)$，若 $\text{HasExclusive}(\Sigma_G(v)) = \texttt{true}$，编译期报诊断错误；
     * 对 `NodeBody::Workflow` 与 `NodeBody::Builtin`：对 `N.body.bindings.input` 中任意引用路径 $p$（其根变量 $\text{RootVar}(p) \notin \text{Injected}(N)$），若其投影后的目标类型满足 $\text{HasExclusive}(\text{TypeOf}(\Sigma_G, p)) = \texttt{true}$，编译期报诊断错误。
   * 诊断码为 `type.exclusive_capture`，地址指向相应 `input` 或 `with.input` 引用；嵌套图继续受已有的显式输入与作用域隔离规则约束。
2. **禁止 Fork-Join 并发组的多条分支同时读取同一独占资源变量**：
   * 在 Pass 4 校验 Fork-Join 并发边 `src -[W_1, W_2, ..., W_k]-> join_target` 时，除检查写集合两两正交 $OutVars(W_i) \cap OutVars(W_j) = \emptyset$ 外，额外检查各分支读取的含独占句柄变量交集 $Use_{\text{excl}}(W_i) \cap Use_{\text{excl}}(W_j)$：
   * 对分支 $W_i$ 的 `body`，按上述 `Run` / `Graph` 的完整输入类型或 `Workflow` / `Builtin` 的投影类型判断外层读取，排除 `Injected(W_i)` 后，将含独占句柄的引用根变量记入 $Use_{\text{excl}}(W_i)$；
   * 若挂载 `for_each`，另行检查 `for_each.source` 的**投影类型**，当且仅当该类型满足 `HasExclusive` 时，将 source 的根变量记入同一集合。该规则独立于 body 种类，覆盖仅通过迭代元素访问句柄的分支，也避免把 `ctx.items` 中的纯标量误判为借用了 `ctx.tx`；
   * 对任意 $1 \le i < j \le k$，必须满足：

     $$
     Use_{\text{excl}}(W_i) \cap Use_{\text{excl}}(W_j) = \emptyset
     $$

   * 若存在交集，在对应并发边上报 `topology.concurrent_exclusive`。静态分析仍按根变量保守拒绝同一结构体中不同独占字段的并发读取；实际资源别名由运行期检查。

### 2.4 编译期字面量不可伪造性与运行期信封校验分离

`handle` 类型区分 **编译期源码字面量校验（`TypeSpec::accepts_literal`）** 与 **运行期传输信封校验（`TypeSpec::accepts_runtime`）**：

1. **编译期源码字面量校验（`accepts_literal`）**：
   * 在编译期所有常量字面量校验点——包括：
     1. `check.rs` 中 `graph.variables.<v>.default` 的默认字面量校验；
     2. `check.rs` 中 `argument()` 对 `use.with.input.<p>` 及内置算子常量参数 `ArgValue::Const` 的校验；
     3. `builtins.rs` 中 `equality()` 对常量比较操作数的校验；
     4. `builtins.rs` 中 `object_merge()` 当 `base` 为常量对象 `{ value: {...} }` 时的校验——
     统一调用 `accepts_literal`：
     * 沿数组元素、映射值和结构体字段递归校验；仅当到达 `handle` 类型位置时，要求字面量为 `null` 且该类型可空，拒绝该位置的一切非 `null` 值；
     * 因而 `array<handle>` 的 `[]`、`map<handle>` 的 `{}` 以及可空句柄字段为 `null` 的合法容器仍可作为字面量，不会因元素类型含有 `handle` 被整体拒绝。
2. **运行期 ABI 信封校验（`accepts_runtime`）**：
   * 在运行期边界（`kernel/mod.rs` 中的 `validate_input` 与 `validate_run_contract`）统一调用 `accepts_runtime`，其中 `handle` 值的标准 JSON 传输信封格式为闭合 JSON 对象：
     ```json
     // 形态 A：纯内存瞬态句柄（如 infra.postgres:Tx，无 locator）
     {
       "$handle": "res_018f...",
       "kind": "infra.postgres:Tx",
       "worker_queue": "axcutor-worker-0123456789abcdef0123456789abcdef",
       "exclusive": true
     }

     // 形态 B：可恢复句柄（如 infra.postgres:Conn 或 infra.s3:TempFile，含非 null 的 locator 与可选 persistent）
     {
       "$handle": "res_018f...",
       "kind": "infra.s3:TempFile",
       "worker_queue": "axcutor-worker-0123456789abcdef0123456789abcdef",
       "locator": {
         "bucket": "axcutor-tmp",
         "key": "wf-123/part-0.parquet"
       },
       "persistent": true
     }
     ```
   * **信封字段规范与校验规则**：
     * **必填键**：`"$handle"`（非空字符串，全局唯一资源 ID）、`"kind"`（满足 `<ModulePath>:<ClassName>` 文法且与目标 `TypeSpec.kind` 精确相等的字符串）、`"worker_queue"`（非空字符串，创建或最近驻留该资源的 Sticky Worker 队列名）；
     * **可选键**：
       * `"exclusive"`：布尔值（默认 `false`）。同一 `rid` 的独占要求只能增强，不能由 Python 输出重标注而丢失；运行期校验同样遵守 `actual.exclusive implies expected.exclusive`。已知独占资源导出到非独占类型位置时产生 `contract` 异常；
       * `"locator"`：任意合法 JSON 值（省略或为 `null` 时表示该句柄为不可跨进程恢复的纯内存瞬态资源；非 `null` 时表示可通过 `@resource` 类的 `cls.restore(locator)` 恢复的可恢复资源，且 `locator` 内部不得嵌套包含 `"$handle"` 信封）；
       * `"persistent"`：布尔值（默认 `false`；仅当 `"locator"` 非 `null` 时允许为 `true`，表示即使所有缓存该资源的 Worker 均已崩溃，仍须在存活 Worker 上通过 `cls.restore(locator)` 恢复并执行 `value.dispose()`）；
     * **闭合约束**：除 `{"$handle", "kind", "worker_queue", "exclusive", "locator", "persistent"}` 外严禁出现任何其他键。
   * **按类型识别与比较**：`OrderedMap` 允许任意字符串键，例如 `map<string>` 中的 `{"$handle": "customer-value"}` 是普通业务数据。扫描器必须同时接收值与其 `TypeSpec`，仅在 `handle` 类型位置识别信封；不得仅根据 `"$handle" in obj` 或完整对象形状推断类型。`builtin:compare (op: eq | ne)` 在句柄类型位置只比较资源 ID，在容器中按对应元素/字段类型递归比较，其他位置保留现有 JSON 值相等性。此运行期比较实现在 `kernel/mod.rs`，编译期 `literal_eq` 保持字面量语义。

---

## 三、 运行时架构：`@resource` Python 资源类协议（`locator` / `restore` / `dispose`）、逻辑堆（`Registry`）与 Sticky Worker 物理存储（`ResourceStore`）

在 Temporal 架构下，Workflow 协程运行在确定性沙箱中（支持随时重放重建状态），而真实的数据库连接、事务、S3 文件句柄和 Agent 会话对象驻留在 Worker 进程的 Activity 执行环境中。为此，Axcutor 采用 **逻辑生命周期与物理对象存储分离、Sticky 亲和性优先与 `kind: <ModulePath>:<ClassName>` 按需加载 `@resource` 类透明恢复相结合** 的架构：

```mermaid
flowchart TB
    subgraph WF["1. Temporal Workflow 确定性沙箱 (workflow.py)"]
        Frames["激活栈帧树 self._frames 与在途返回值保活槽 (持有 Handle 信封)"]
        PrefQ["首选亲和性队列映射 self._preferred_worker_queues (base_queue -> worker-uuid)"]
        LogicalHeap["确定性逻辑堆 self._registry (rid -> kind, base_queue, resident_queues, locator, persistent, ownership, external_disposed, ref_count)"]
        Frames -->|"返回值接收 / 赋初值 / 写回 / kill_vars / 弹栈 触发 inc_ref 与 dec_ref"| LogicalHeap
    end

    subgraph W1["2. Temporal Worker 实例 (主 Worker + 附属 Sticky Worker)"]
        SharedQ["主 Worker 监听共享队列: base_queue"]
        StickyQ["附属 Sticky Worker 监听专属队列: worker-uuid"]
        ResLoader["@resource 类按需解析器 (复用 module_mounts 按 kind='<module>:<ClassName>' 加载)"]
        Store["进程内物理资源表 ResourceStore (rid -> value, kind, locator, dispose, persistent, closing, active_attempt_id, lock)"]
        ExecRun["Activity: execute_run"]
        Dispose["Activity: dispose_resources"]
        ExecRun --> Store
        Dispose --> Store
        Store -->|"本地未命中且含 locator 时按 kind 加载 @resource 类并调 cls.restore(locator)"| ResLoader
    end

    WF -->|"优先路由至 StickyQ 复用热缓存"| StickyQ
    WF -.->|"StickyQ 宕机超时且所有输入句柄均含 locator 时透明回退至 SharedQ"| SharedQ
    LogicalHeap -->|"ref_count == 0 时优先按 resident_queues 调度 dispose_resources"| StickyQ
    LogicalHeap -.->|"persistent=true 且未确认 external_disposed 时回退 SharedQ 保底清理"| SharedQ
```

### 3.1 `@resource` Python 类三要素协议（`locator` / `restore` / `dispose`）与 `kind: <ModulePath>:<ClassName>` 自寻址机制

不设宿主 `create_worker` 资源注册表，也不支持模块顶层裸函数写法或 `__resource__` 特殊变量；所有外部资源（无论瞬态资源还是可恢复资源）统一在挂载的 Python 模块中通过 **`@resource` 类装饰器**定义，并通过 `kind: "<ModulePath>:<ClassName>"` 自寻址：

```python
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


def resource(
    cls: type[T] | None = None,
    *,
    persistent: bool = False,
) -> type[T] | Callable[[type[T]], type[T]]:
    """将一个 Python 类声明为可按 `<ModulePath>:<ClassName>` 寻址的资源类。"""
    ...
```

1. **`@resource` 资源类的三要素方法契约**：
   * 装饰器校验目标 `cls` 必须为模块顶层 Python `class`（禁止装饰函数、实例或嵌套局部类），且检查以下三个可选/必填成员方法（不满足约束时在类定义期直接抛 `TypeError`）：
     * **要素 1：定位符提取方法 `locator(self) -> Any | Awaitable[Any]`**（可选，定义时必须与 `restore` 成对出现）：
       * 定义在类上的实例方法 `def locator(self) -> JsonValue:`（允许同步或异步）；
       * 当调用 `await resources.put(instance)` 注册实例时，由运行时自动调用 `instance.locator()` 提取可跨 Worker 传输的 JSON 定位符（若类未定义 `locator` 方法，或 `instance.locator()` 返回 `None`，则该实例被注册为无 `locator` 的纯内存瞬态资源）。
     * **要素 2：跨 Worker 恢复类方法 `restore(cls, locator) -> T | Awaitable[T]`**（与 `locator` 成对出现；若 `persistent=True` 则 `locator` 与 `restore` 均必填）：
       * 定义为 `@classmethod async def restore(cls, locator: ...) -> Self:`（允许 `@classmethod` 或 `@staticmethod`，支持同步或异步）；
       * 当句柄漂移到未缓存该 `rid` 的 Worker（或共享队列保底清理）时，接收 JSON 反序列化后的只读 `locator`（深拷贝隔离，且与 `builtin` 输入一致将传输层 `Decimal` 递归转换为原生 `float`），在当前 Worker 上恢复并返回该资源类的实例 `value`；运行期必须校验 `type(value) is cls`，返回其他类或子类的实例均抛 `TypeError`。该规则适用于 `get`、显式关闭与被动清理中的全部恢复路径，校验通过后才允许缓存或使用该实例。
     * **要素 3：析构实例方法 `dispose(self) -> Any | Awaitable[Any]`**（当 `persistent=False` 时可选，当 `persistent=True` 时**必填**）：
       * 定义在类上的实例方法 `async def dispose(self) -> None:`（支持同步或异步）；
       * **唯一的析构钩子**：无论该实例 `self` 是最初在当前 Worker 上通过 `resources.put(instance)` 注册的，还是由 `cls.restore(locator)` 在当前 Worker 上恢复出来的，销毁时统一调用 `await self.dispose()`；
       * 方法必须能容忍清理重试及其他副本已删除外部实体，并在 `finally` 中释放本实例持有的流、连接或本地缓存；运行时不提供 exactly-once 副作用保证。`locator`、`restore` 与 `dispose` 均允许同步返回值或 awaitable，下文的 `await ...` 为简写，实际实现只对 `inspect.isawaitable(result)` 的结果执行 `await`。
   * **`persistent: bool = False`**：
     * 当 `persistent == False`（默认，如数据库事务 `infra.postgres:Tx` 或连接 `infra.postgres:Conn`）：表示资源的生命周期附着于 **进程内的连接/状态实例**。若该资源含非空 `locator`，原 Worker 崩溃后后续业务节点可在新 Worker 上通过 `cls.restore(locator)` 恢复连接继续运行；但当引用计数归零进入被动回收时，仅需在当前实际缓存了该实例的存活 Worker 上执行 `value.dispose()`——若持有它的 Worker 已经崩溃，连接已随进程消亡断开，**绝不在新 Worker 上为了执行 `dispose` 而调用 `cls.restore(locator)` 白白新建一条连接再立即关闭**；
     * 当 `persistent == True`（如 S3 临时文件 `infra.s3:TempFile`）：要求类必须同时定义 `locator`、`restore` 与 `dispose`（且运行时 `instance.locator()` 不得返回 `None`）。表示资源的生命周期附着于 **外部持久化存储实体**，原 Worker 进程崩溃并不会使 S3 上的临时文件消失。当引用计数归零进入被动回收时，若所有曾缓存该对象的 Sticky Worker 均未能确认完成清理（`external_disposed == False`），Workflow 必须将回收请求回退投递至共享主队列 `base_queue`，由任意存活 Worker **按 `kind` 加载该 `@resource` 类，先调用 `cls.restore(locator)` 恢复出 `value`，再调用 `value.dispose()`** 完成外部临时文件清理。
   * 装饰器在类对象上写入内部只读元数据属性 `__axcutor_resource_meta__ = ResourceMeta(persistent=persistent, has_restore=...)`。

2. **按 `kind: <ModulePath>:<ClassName>` 复用 `module_mounts` 的确定性双向解析协议（结合 RFC-0014 与 RFC-0015）**：
   * Worker 之间不传递内存中的 Python 函数或类对象；`RunActivities` 与 `ResourceStore` 共享 `create_worker(..., module_mounts=...)` 配置的根包挂载表 `module_mounts: Mapping[str, str] | None`。
   * **正向解析（由 `kind: "<ModulePath>:<ClassName>"` 加载 `@resource` 类 `cls`）**：
     当 Worker 在 `resources.get`、`resources.close` 或 `dispose_resources` 中需要按 `kind`（如 `"infra.s3:TempFile"`）加载资源类时：
     1. 按 `module_path, sep, class_name = kind.partition(":")` 拆分，校验 `package_path(module_path)` 与 `package_segment(class_name)`；
     2. 提取根包段 `root, dot, suffix = module_path.partition(".")`：
        * 若 Worker 配置了显式 `module_mounts`，则校验 `root in module_mounts`（未挂载则抛 `LookupError(f"no Python package mounted for {root!r}")`），映射后的物理模块路径为 `target_module = (module_mounts[root] if module_mounts is not None else root) + dot + suffix`；
     3. 调用与 [`module.py:_load_module`](../python/axcutor_runtime/src/axcutor_runtime/module.py) 同构的无二义性模块加载器加载 `mod = _load_resource_module(target_module)`（拒绝同名目录包 `foo/__init__.py` 与文件模块 `foo.py` 并存的二义性；支持 `target_module` 为普通 `.py` 文件模块或包目录 `__init__.py`）；
     4. 从模块中读取 `cls = getattr(mod, class_name, None)`，校验 `isinstance(cls, type)`、具有 `@resource` 标记 `__axcutor_resource_meta__`、且 `cls.__qualname__ == class_name`（确保是模块顶层类且未被重名覆盖），返回该资源类。
   * **反向推导（在 `resources.put(instance)` 中由 `cls = type(instance)` 推导规范逻辑 `kind`）**：
     1. 校验 `cls` 具有 `@resource` 标记 `__axcutor_resource_meta__` 且 `cls.__qualname__ == cls.__name__`；
     2. 读取 `physical_module = cls.__module__`：若配置了 `module_mounts`，将 `physical_module` 的挂载目标前缀逆映射回逻辑根包名 `root` 得到 `logical_module`（若不属于任何已挂载根包则抛 `LookupError`）；未配置 `module_mounts` 时 `logical_module = physical_module`；
     3. 合成 `kind = f"{logical_module}:{cls.__name__}"`，并校验正向解析 `resolve_resource_class(kind) is cls`（确保该类可通过 `kind` 在任意其他 Worker 上无歧义加载）。
   * **运行期类身份不变量**：在每个 Worker 进程内，活跃物理资源必须满足 `type(entry.value) is resolve_resource_class(entry.kind)`。注册、缓存命中后交付业务、恢复后缓存或执行析构均须核对这一精确类身份；类名相同、结构兼容或 `isinstance` 成立均不能替代 `is` 检查。跨 Worker 通过规范逻辑 `kind` 与各自一致的挂载配置解析类，不跨进程比较类对象。

3. **执行 `locator`、`restore` 与 `dispose` 时的上下文隔离**：
   * 在 `resources.put(instance)` 中先调用 `instance.locator()`，校验并深拷贝其 JSON 表示，再转移资源所有权。`cls.restore` 在失败或取消时负责释放尚未交给 Store 的半构造对象。调用方提供的 `instance` 在 `put` 成功注册前由调用方负责，成功注册后由当前 Attempt 负责。
   * 执行 `instance.locator`、`cls.restore` 或 `instance.dispose` 时临时禁用 `resources` 上下文（将 `_current_resource_context` 置为不可调用状态），并在退出时恢复。方法可以直接使用普通 Python 对象和第三方 SDK，但不得嵌套调用 `resources.put/get/close/discard`，避免在资源初始化锁内递归借用，或在析构阶段创建无法移交的新句柄。

#### 3.1.1 `resources.put(instance, *, exclusive=False)` 的单实例内聚注册接口

得益于 `@resource` 类自身封装了 `locator(self)`、`restore(cls, locator)` 与 `dispose(self)`，`ResourceContext.put` 收敛为直接接收 **`@resource` 类的实例 `value`** 的极简接口，彻底无需调用方手写 `kind` 字符串或手动传递 `locator` / `dispose` 参数：

```python
class ResourceContext:
    async def put(
        self,
        value: T,
        *,
        exclusive: bool = False,
    ) -> Handle[T]:
        """将一个 @resource 类的实例注册进当前 Worker 的 ResourceStore 并返回强类型 Handle。"""
        ...
```

1. **统一注册流程**：
   * 校验 `cls = type(value)` 必须是由 `@resource` 装饰的顶层资源类（否则抛 `TypeError`），并按第 3.1 节反向推导并校验其规范 `kind = "<ModulePath>:<ClassName>"`；
   * **自动提取定位符 `locator`**：
     * 若 `cls` 定义了 `locator` 方法：调用 `raw_locator = await value.locator()`；
     * 若 `cls` 未定义 `locator` 方法：令 `raw_locator = None`；
   * **自动区分瞬态资源与可恢复资源**：
     * **当 `raw_locator is None`（纯内存瞬态资源，如 `infra.postgres:Tx`）**：
       * 若 `cls.__axcutor_resource_meta__.persistent == True`，立即抛出 `ValueError("persistent @resource instance must return a non-null locator")`；
       * 在当前 Worker 的 `ResourceStore._entries[rid]` 中存入 `value` 及其绑定的 `value.dispose`（若定义了 `dispose`），返回 `"kind": kind, "locator": None, "persistent": False` 的 `Handle`；
     * **当 `raw_locator is not None`（可恢复资源，如 `infra.postgres:Conn` 或 `infra.s3:TempFile`）**：
       * 校验 `cls` 必须同时定义了 `restore` 类方法，且 `raw_locator` 为不含嵌套 `"$handle"` 的合法 JSON 值（深拷贝并归一化 `Decimal -> float`）；
       * 在当前 Worker 的 `ResourceStore._entries[rid]` 中存入 `value`、`locator`、`value.dispose` 与 `persistent = cls.__axcutor_resource_meta__.persistent`，返回携带 `locator` 与 `persistent` 的 `Handle`。

`exclusive` 记录资源本身的最低访问限制，存入物理条目及信封。Activity 边界同时结合输入、所选输出的类型位置，按 `rid` 汇总独占要求；任一已知位置或物理条目为独占时，该 `rid` 的规范状态即为独占，重导出必须保留，所有别名共同受约束。先汇总、再检查输出，不能因字段遍历顺序不同而允许降级。

---

### 3.2 Worker 进程亲和性、Sticky 路由与跨 Worker 故障恢复协议

每个 Worker 在启动时（`create_worker`）与 Workflow 调度时遵循以下双队列亲和性与分级故障转移协议：

1. **Worker 专属队列标识（`worker_queue`）与双 `Worker` 协同装配**：
   * 每个持有 `ResourceStore` 的 Worker 进程分配一个唯一专属队列名 `worker_queue = f"{task_queue}-worker-{uuid4().hex}"`（其中前缀 `{task_queue}-worker-` 显式编码了该 Worker 所属的主共享队列名 `base_queue = task_queue`）；
   * 从专属队列名末尾的固定 `-worker-<32 hex>` 后缀提取 `base_queue`，不能在队列名中任意切分 `-worker-`。外部传入句柄也必须符合此寻址格式；队列名只用于寻址，不是存活证明。
   * 由于 `temporalio.worker.Worker` 单个实例仅绑定单个 `task_queue`，`create_worker` 内部封装 **主队列 `Worker`**（监听共享 `task_queue`，注册 `AxcutorInterpreterWorkflow` 与 `execute_run`、`dispose_resources`）与 **附属 Sticky `Worker`**（监听 `worker_queue`，共享同一个 `ResourceStore`、`module_mounts` 与 `RunActivities` 实例，注册 `execute_run` 与 `dispose_resources`），并在 `run()` / `__aenter__` / `__aexit__` 生命周期中协同启动与优雅关闭二者。
   * `create_worker` 返回明确的复合 Worker 对象，公开 `run()`、`shutdown()` 和异步上下文管理接口；任一成员启动失败时关闭已启动成员，正常关闭时先停止接收业务、排空 Activity，再关闭共享 Store。附属 Worker 不注册 Workflow；配置按 Workflow / Activity 职责分配，不以继承或全量代理伪装成单个 SDK Worker。
   * `_parse_options` 增加正整数选项 `sticky_schedule_to_start_timeout_ms`（默认 `5000`）与 `resource_disposal_timeout_ms`（默认 `30000`）。前者限制一次 Sticky 排队等待，后者限制一次被动清理的总等待；这些选项随 Workflow 输入记录并传给 Child，不读取进程环境改变重放行为。
2. **Workflow 级首选 Sticky 队列（`self._preferred_worker_queues`）与 `execute_run` 分级路由规则**：
   * `AxcutorInterpreterWorkflow` 维护按主队列 `base_queue` 索引的首选 Sticky 队列映射 `self._preferred_worker_queues: dict[str, str]`（在启动 Child Workflow 时一并通过输入上下文继承）；
   * 在 `AxcutorInterpreterWorkflow._run_activity` 调度节点（设该节点 executor 对应的主共享队列为 `base_queue = options.task_queues.get(executor, default_task_queue)`）时，扫描当前节点 `input_snapshot` 中包含的所有 Handle 信封，并按 `locator is None` 将输入句柄划分为 **瞬态绑定句柄集** $H_{\text{pin}}$ 与 **可恢复句柄集** $H_{\text{rec}}$：
     * **情形 1：包含瞬态绑定句柄**（$H_{\text{pin}} \neq \emptyset$，如含有 `infra.postgres:Tx`）：
       * 提取 $H_{\text{pin}}$ 中所有句柄所属的 `worker_queue` 集合 $Q_{\text{pin}}$；
       * 若 $|Q_{\text{pin}}| > 1$（存在两个驻留于不同 Worker 内存且均不可恢复的瞬态句柄）：立即抛出可恢复的 **`execution`** 异常（跨 Worker 瞬态内存句柄冲突）；
       * 若 $Q_{\text{pin}} = \{w_{\text{pin}}\}$ 且该队列属于目标 `base_queue`：将 `execute_run` 路由至 $w_{\text{pin}}$（带上述排队期限）。其他可恢复输入可在此通过对应的 `@resource` 类 `cls.restore(locator)` 恢复；若瞬态句柄所在 Worker 不属于目标 executor 的队列池，则报可恢复的 **`execution`** 路由冲突，不覆盖 executor 路由。
       * 若发往 $w_{\text{pin}}$ 的 `execute_run` 触发 `schedule_to_start_timeout`：只说明本次任务未及时被领取，也可能是 Worker 忙碌。清除本次首选绑定并报可恢复的 **`execution`** 异常，但保留该 Worker 的驻留记录与清理责任；不得推断内存已消失。外层事务子图可按业务幂等约束重新开始。
     * **情形 2：仅包含可恢复句柄**（$H_{\text{pin}} = \emptyset \land H_{\text{rec}} \neq \emptyset$，如仅含 `infra.postgres:Conn` 和/或 `infra.s3:TempFile`）：
       * **热缓存亲和性优先（Sticky-First）**：候选仅限目标 `base_queue` 的 Worker。先取该池的首选队列；若无首选，则按类型扫描输入句柄的确定性顺序，取第一个句柄的最新兼容驻留队列；均无候选时直接投递目标共享队列。不依赖在线探测、集合遍历顺序或进程本地状态选队列。
       * **未启动任务的透明回退（Transparent Failover & Rehydration）**：若发往 $w_{\text{sticky}}$ 的任务触发 **`schedule_to_start_timeout`**，因该次任务尚未启动且全部输入可恢复，Workflow 执行以下回退：
         1. 清除该次路由的首选绑定，但保留所有 `resident_queues` 与清理责任；
         2. **立即透明重投递 `execute_run` 至共享主队列 `base_queue`**；
         3. 存活的新 Worker $w_{\text{new}}$ 认领任务后，在业务代码调用 `await resources.get(h)` 时按 `h.kind` 加载 `@resource` 类并通过 `cls.restore(h.locator)` 透明恢复资源并缓存至 $w_{\text{new}}$ 的 `ResourceStore`；
         4. 消费 Activity 生命周期元数据后，将 $w_{\text{new}}$ 记入驻留队列并更新首选绑定。已开始执行后的超时不能按此规则透明重投，须进入第 3.3 节的重试与借用收敛规则。
     * **情形 3：无输入句柄**（$H_{\text{pin}} = \emptyset \land H_{\text{rec}} = \emptyset$）：
       * 若 `base_queue in self._preferred_worker_queues`：优先路由至 `self._preferred_worker_queues[base_queue]`；若触发 `schedule_to_start_timeout`，自动清除该失效绑定并立即透明回退投递至共享队列 `base_queue`；
       * 若尚未绑定首选队列：直接投递至共享队列 `base_queue`。

---

### 3.3 Workflow 侧：确定性引用计数逻辑堆（`self._registry`）与双路容错回收

在 `python/axcutor_runtime/src/axcutor_runtime/workflow.py` 的 `AxcutorInterpreterWorkflow` 中，维护逻辑堆表 `self._registry: dict[str, LogicalResourceEntry]`：

```python
@dataclass
class LogicalResourceEntry:
    resource_id: str
    kind: str
    base_queue: str
    resident_queues: list[str]         # 已登记该 rid 驻留责任、尚未确认清理的 Sticky 队列；排队超时不删除
    exclusive: bool = False            # 按 rid 合并的最低访问限制，只能增强
    locator: Any = None                # 可序列化恢复定位符（None 表示不可跨进程恢复的纯内存瞬态资源）
    persistent: bool = False           # True 表示外部实体仍需共享队列保底清理
    ownership: Literal["borrowed", "owned"] = "owned"
    disposed: bool = False             # 若已在节点内通过 close/discard 显式终结则为 True
    external_disposed: bool = False    # persistent 资源已有一次完整析构成功确认；不代表所有本地副本已释放
    ref_count: int = 0
```

逻辑堆完全由 Workflow 协程内的确定性事件驱动，在 Temporal 重放（Replay）期间按完全相同的顺序重建，**无需任何外部数据库**：

1. **句柄扫描器 `_iter_handles(value: Any, typespec: TypeSpec) -> Iterator[JsonObject]`**：
   * 按 `TypeSpec` 递归进入数组元素、映射值与结构体已声明字段，仅在非空 `handle` 类型位置产出信封，不扫描信封的 `locator`。扫描环境或输出映射时，逐一配对每个字段与其 schema；未通过契约校验的对象不能用键名猜测资源类型。
   * 注意：若同一个 `Frame.env` 中的两个不同变量（或数组中的两个槽位）指向同一个 `resource_id`，按出现次数精确累计引用计数。
2. **所有权分类（`ownership`）、跨 Worker 缓存追踪与跨 Workflow Run（含 Child Workflow）边界**：
   * **`borrowed`（外部借入）**：Run 初始输入中的句柄注册为 `ownership="borrowed"`，不因当前 Run 的引用归零而被动销毁外部实体；业务显式 `close` / `discard` 仍是对该资源的终结操作，必须向所有者同步。新增副本的驻留责任须登记给所有者，由所有者在全局 RC 归零后统一回收；不因借入方某次 Attempt 失败而提前析构仍被引用的资源。
   * **`owned`（内部受管）**：当前 Workflow Run 运行期间由 `run` 节点或 Child Workflow 调用新产出（首次出现在 Activity / Child Workflow 返回值中且不在当前 `self._registry` 内）的句柄，注册为 `ownership="owned"`，初始 `resident_queues = [handle["worker_queue"]]`。
   * **跨 Worker 恢复与显式关闭/注销状态同步**：
     * Activity 的成功响应和可协作完成的失败响应都携带第 3.4.4 节的资源元数据；Workflow 在处理业务结果、`retry` / `catch` 或丢弃返回值之前先应用元数据。`rehydrated_rids` 记录本次恢复事件；`resident_rids` 记录本次结束时仍存活的相关缓存条目，Workflow 据此合并驻留，不能把已就地释放的副本再次登记为驻留，也不能漏掉原已缓存的输入资源。
     * 对成功 `close` 的 `closed_rids` 与成功 `discard` 的 `discarded_rids`，将逻辑条目标记 `disposed=True` 并从 `resident_queues` 中移除已完成本地摘除的队列：
       * **`discard` 仅限无 `locator` 的纯内存瞬态资源**：由于 `locator is None` 的瞬态资源全局仅存在于创建它的单一 Sticky Worker 上，在原 Worker 完成 `discard` 本地摘除后，`resident_queues` 即刻归空，绝不存在任何跨 Worker 残留副本，也无需任何 `evict_only` 跨节点协议；
       * **`close` 后其余驻留副本的统一析构**：对含 `locator` 的可恢复资源，在某 Worker 上成功 `close` 后，其余 `resident_queues` 中的驻留副本仍统一调用同一个幂等 `value.dispose()` 释放各自的物理对象与本地流；外部实体已被删除时也必须通过 `finally` 完成本地释放。
     * `disposed` 只表示逻辑资源终结，不表示所有物理缓存已释放。显式终结一经同步，先禁止新借用；其余在途借用排空后，立即把残留副本送入待清理队列，无需等待旧别名的 `ref_count` 归零。保留逻辑终结状态直到所有活跃引用和在途借用消失。后续 `get` 报 `execution`、再次导出报 `contract`，重复 `close` 幂等返回，不能凭旧别名中的 `locator` 复活。
     * 对 `persistent=True` 的资源，成功 `close` 或 `dispose_resources` 返回 `disposed` 时，将 `external_disposed=True` 单调置位；`missing`、排队超时和析构失败都不能推断外部删除成功。该确认随生命周期更新和清理状态保留，避免剩余副本未响应时再次为已确认删除的实体调用 `cls.restore(locator)`。
     * 同一 `rid` 的 `kind`、`locator`、`persistent` 与原始 `base_queue` 不得被重导出改写，`worker_queue` 仅为驻留提示；独占要求取已知状态的逻辑或。检查身份一致性后再合并驻留，拒绝冲突元数据。
   * **当前 Workflow Run 顶层正常导出豁免（`parent_id is None`）**：当 `AxcutorInterpreterWorkflow.run()` 的入口栈帧（`frame.parent_id is None`，涵盖根工作流 `call_depth == 1` 以及作为独立 Temporal Child Workflow 运行的子工作流 `call_depth > 1`）正常到达顶层 `end` / `end.<branch>` 返回最终 `result["output"]` 时，凡出现在最终 `result["output"]` 中的 `owned` 句柄，其所有权移交给上层调用方（在当前 Run 中标记为 `ownership="borrowed"` 豁免销毁）：
     * 若当前为 Child Workflow，父 Workflow 接管新导出的句柄及其完整驻留信息。对于 Child 借入的句柄，驻留、独占性与终结状态的同步**独立于业务输出**：即使 Child 只返回标量、输出被投影丢弃或 Child 失败，也必须将已发生的生命周期变化交还父所有者。内部 `resource_update` 消息以来源 Run、来源调用标识和单调序号去重；Child 在协作退出前等待更新被父 Run 的 Temporal 历史持久化接收，父在释放该调用的借用前应用更新。此处不要求父的业务主流程返回或逐级发回应用层 ACK；元数据由解释器构造，不读取业务返回的同名字段。
     * 若当前为根工作流，宿主调用方接收独立的资源移交描述，包含新导出资源的完整身份、原始 `base_queue`、所有尚未清理的 `resident_queues`、独占性，以及借入资源的生命周期更新。不能只交付一个 `worker_queue` 提示便丢弃其他副本的清理责任。
3. **无真空期的在途返回值保活与栈帧引用计数协议（RC Protocol）**：
   * **Activity / Child Workflow 返回值落地即纳入逻辑堆（消除在途真空期）**：
     * 一旦 Activity / Child 返回内部响应，先解包 `result` 并应用资源身份和生命周期元数据，在任何可能挂起、业务校验或输出投影之前，对 `result["output"]` 按 schema 扫描的所有句柄执行注册与临时 `inc_ref`；接管与保活在同一同步步骤完成。
     * 该临时保活引用由调用栈帧通过 `try ... finally` 托管：只有当返回值被正式绑定写入外层 `frame.env`（完成目标变量 `inc_ref`）或作为 `RootBody::Run` 顶层结果返回后，才在 `finally` 中释放该在途临时 `inc_ref`；
     * 因此，即便在 `_run_activity` 随后的 `_contract` 校验中失败、在 `for_each` / `fork_join`（`_owned_group`）中因兄弟分支失败或 `cancel_and_drain` 而丢弃了已完成的分支返回值、或者在 `NodeBody::Workflow` 的 `bindings["output"]` 中仅映射了子图/子工作流返回字段的子集（忽略了其余含句柄的字段），`finally` 中对原始返回值的配对 `dec_ref` 都会精确将未被接收句柄的 `ref_count` 减至 `0` 并立即回收，**绝无任何在途泄漏窗口**。
   * **栈帧创建（`_frame`）**：对实际拷贝建立的 `frame.env` 统一执行 `inc_ref`，退出时只对该帧当前仍持有的值配对 `dec_ref`。迭代诊断帧保留的完整父视图也计入引用；未创建帧的排队或取消协程不得减引用。诊断视图的保活不等于业务代码拥有读取或独占借用权限，借用只按实际输入计算。
   * **边级抹除（`kill_vars`）**：在 `_exec_graph` 中执行 `frame.env.pop(name, None)` 时，对被移除旧值中的所有句柄执行 `dec_ref`，并立即 `await self._flush_disposals()`。
   * **节点输出写回（`frame.env.update`）**：当节点返回 `result["output"]` 合并入 `frame.env`（或 `for_each` / `fork_join` 屏障汇聚写回）时，**遵循“先 `inc_ref` 新值、再 `dec_ref` 被覆盖旧值、最后释放临时保活槽并 `await self._flush_disposals()`”的顺序**（防止同名变量自我赋值 `tx = tx` 时引用计数瞬间归零误触发销毁，同时确保循环内覆写的旧句柄在当步立即释放而不必等到退环）。
   * **栈帧退出与弹出（`_exec_graph` / `_exec_root` 的 `finally`）**：
     * 若栈帧正常返回 `return_output` 给父调用方：先对 `return_output` 中的句柄执行一次临时 `inc_ref`（移交调用方保活槽），然后对当前 `frame.env` 中的全部变量执行 `dec_ref` 销毁当前帧，待父栈帧接收映射完字段后再在父调用方的 `finally` 中释放该保活槽；
     * 若栈帧因 `-> fail`、未捕获异常或 `CancelledError` 异常展开（Unwind）：直接对当前 `frame.env` 中的全部句柄执行 `dec_ref`，并调用 `await self._flush_disposals()`。
4. **引用归零时的确定性异步清理（`_flush_disposals` 端到端 `noexcept` 与 `persistent` 共享队列保底协议）**：
   * 被动回收在 `ownership == "owned"`、`ref_count == 0` 且没有在途借用时入队；显式 `close` 则按上述终结协议在借用排空后立即将剩余驻留副本送入待清理队列，借入资源的显式终结也适用。不能因 `disposed=True` 就跳过残留副本，也不能在清理确认前丢弃其寻址信息；待清理队列本身承担清理期间的保活责任。
   * Worker 中同一 `rid` 的缓存副本受该逻辑资源的所有权与 RC 管理，缓存本身不增加逻辑引用；资源仍被引用时不做自动提前淘汰。因此允许副本持有需要关闭的流或本地缓存，无需为被动 RC 回收额外引入本地释放钩子。
   * `_flush_disposals` 持有明确的清理任务引用，在协程收到重复取消时循环等待同一个被 `shield` 保护的任务，待有限清理预算结束后再恢复原始业务异常或取消。单次 `await asyncio.shield(...)` 只保护内部任务，不能保证外层等待完成（参见 [Python shield 文档](https://docs.python.org/3.11/library/asyncio-task.html#shielding-from-cancellation)）。预期的清理失败只记录诊断，不改变 DSL 异常选择器；不能用笼统吞错掩盖引擎不变量破坏。
     * **对 `persistent == False` 的资源（数据库事务 `infra.postgres:Tx`、数据库连接 `infra.postgres:Conn`）**：
       * 向 `entry.resident_queues` 中的每个 Sticky 队列并发调用 `dispose_resources(rids=[rid])`（带短 `schedule_to_start_timeout`）；
       * 存活的 Worker 在本地 `ResourceStore._entries` 中找到 `rid`，调用 `await value.dispose()` 关闭连接/回滚事务并移除条目；
       * `schedule_to_start_timeout` 可能由 Worker 繁忙引起，不能证明连接已断（参见 [Temporal 超时定义](https://docs.temporal.io/encyclopedia/detecting-activity-failures#schedule-to-start-timeout)）。此时保留未确认清理项，在后续清理点按有限预算重投；绝不为关闭进程内连接而在共享队列调用 `restore`。只有明确确认原进程终结，才可将其物理副本视为已消失。
     * **对 `persistent == True` 的外部持久化临时资源（如 S3 临时文件 `infra.s3:TempFile`）**：
       * 向所有已登记驻留队列发送 `dispose_resources`，每个存活副本都调用同一个 `value.dispose()`，完成外部删除及本实例的本地释放；不能只让一个副本析构、其余副本直接摘除。
       * 外部文件只有一份，多个副本及重试可能重复发起删除；`@resource` 类的 `dispose(self)` 必须以幂等方式处理“已不存在”，并保证外部删除失败或重复时仍在 `finally` 中释放本地资源。
       * **共享队列保底回退（Fallback to Shared `base_queue`）**：仅当 `external_disposed=False`，且驻留列表为空或驻留清理失败、未及时领取时，在预算内向 `entry.base_queue` 发送携带 `resource_id`、`kind`、`locator` 与 `rehydrate_for_dispose=True` 的请求。已有外部清理成功确认（`external_disposed=True`）后，只继续释放尚存的本地副本，不再为它们重新恢复对象；共享队列删除成功不能代替其他存活副本的本地释放。
       * 集群中 **任意存活的 Worker** 从共享队列 `base_queue` 认领该任务后，按 `kind: <ModulePath>:<ClassName>` 加载 `@resource` 类 `cls`，先调用 `value = await cls.restore(deepcopy(locator))` 恢复出实例，随即调用同一个 `noexcept` 的 `await value.dispose()` 删除 S3 临时文件！
   * `dispose_resources` 返回每项的 `disposed` / `missing` / `failed` 状态；业务析构异常被记录，不伪装成成功。预算耗尽的未确认项在 Run 存续期间保留；Run 最终退出仍无法确认时输出结构化清理诊断，外部持久化对象依赖 TTL 或宿主补偿。`noexcept` 表示不改变业务控制流，并不等于删除必然成功或无限等待。

5. **按实际资源 ID 的调度前借用与重试边界**：
   * Workflow 根据实际输入及其类型位置，结合 Registry 的规范独占状态，按 `rid` 建立活动借用表。同一资源任一活动借用要求独占时，另一个并发调用的借用立即报 `execution`，而不是排队；无独占要求时可共享。该检查覆盖不同变量名、容器重复元素及路由到不同 Worker 的情况。
   * 借用作用域覆盖整个节点调用及其子调用，随 Child 上下文委派；顺序嵌套调用可在父作用域内继续借用，同一作用域下的并发兄弟仍须互斥。完成取消并排空后才能释放，不能只在发出取消请求时释放。多个独立根 Workflow 借用同一外部句柄时，由外部所有者协调；单个 Run 的 Registry 不提供跨独立所有者的分布式锁。
   * 输入或输出 schema 递归包含 `handle` 的节点禁用 `native_retry`，单次 Activity 的 `maximum_attempts=1`，由 Workflow 在应用本次生命周期元数据后推进 DSL 重试。无句柄节点保留原有优化。
   * Server 的执行超时不证明旧 Attempt 已停止；未获得协作排空确认时，不释放受影响资源的独占借用并重试复用它。旧 Attempt 无法收敛时，当前借用作用域失败；外层只能在业务允许时用新资源重试。外部 Session 的服务端并发、副作用与提交结果仍需 `@resource` 类的幂等或 fencing 协议保障，进程内锁不能消除网络分区中的旧执行者。

---

### 3.4 Activity 与 Worker 侧：物理资源表（`ResourceStore`）、按需透明恢复与显式/被动析构语义

在 `python/axcutor_runtime/src/axcutor_runtime/resources.py` 中实现进程内 `ResourceStore`、`@resource` 装饰器与通过 `ContextVar` 暴露的 `ResourceContext`：

```python
class ResourceContext:
    """注入到 builtin 与 module Python 执行器中的 `resources` 上下文对象。"""

    async def put(
        self,
        value: T,
        *,
        exclusive: bool = False,
    ) -> Handle[T]:
        """将一个 @resource 类实例注册进当前 Worker 的 ResourceStore（自动调用 value.locator() 提取定位符），返回可写入 Output 的 Handle。"""
        ...

    async def get(self, handle: Handle[T] | JsonObject) -> T:
        """根据 Handle 解引用获取底层物理 @resource 实例（唯一解引用入口）。
        若当前 Worker 本地未命中且 handle 含非空 locator，自动按 handle.kind 加载 @resource 类并调 cls.restore(locator) 恢复缓存；
        若不存在且不可恢复、已被显式关闭/丢弃、正在关闭中或被另一并发独占借用，则抛 RuntimeError。
        """
        ...

    def discard(self, handle: Handle[Any] | JsonObject) -> None:
        """仅限无 locator 的纯内存瞬态资源：将该 Handle 从活跃物理表中直接移除（不触发 dispose，幂等且无返回值）；
        若对含非空 locator 的可恢复资源调用，或该条目正在关闭中，则抛 RuntimeError。
        """
        ...

    async def close(self, handle: Handle[Any] | JsonObject) -> None:
        """显式执行该 Handle 的 dispose 方法（若本地未命中且 persistent=True 则先调 cls.restore(locator) 恢复再执行）；
        本地没有实例的非 persistent 活跃资源报错，不伪报关闭成功；
        仅当 dispose 成功完成时摘除当前实例并标记逻辑终结，其余副本进入被动清理（已终结时幂等）。
        """
        ...
```

#### 3.4.1 物理资源条目（`ResourceEntry`）的无墓碑状态机、按需恢复与运行期独占借用保护

一旦某个资源通过 `discard`、`close` 或 `gc_dispose` 完成销毁，它就会 **直接从 `ResourceStore._entries` 字典中真实移除（`self._entries.pop(rid, None)`）**，不在 `ResourceStore` 中永久残留任何 `closed` 或 `removed` 墓碑对象（Tombstone），使底层 Python 对象得以立即被垃圾回收（单次 Activity Attempt 内部仅维护轻量级集合 `attempt_closed_rids: set[str]` 用于拦截同一 Attempt 内的悬垂导出与关后重读）。

驻留在 `_entries` 字典中的活跃条目 `ResourceEntry` 维护以下状态字段：

* **`closing: bool = False`** 与 **`lock: asyncio.Lock`**：保护异步 `cls.restore(locator)` 初始化与 `await value.dispose()` 析构挂起期间的临界区；
* **`active_attempt_id: str | None = None`**：标记当前本地独占借用者，与 Workflow 调度前检查配合；该字段只约束本进程，不能作为跨 Worker 的锁。

```python
@dataclass
class ResourceEntry:
    resource_id: str
    kind: str
    value: Any
    exclusive: bool = False
    locator: Any = None
    dispose: Callable[[], Any | Awaitable[Any]] | None = None
    persistent: bool = False
    closing: bool = False
    active_attempt_id: str | None = None
    borrowing_attempt_ids: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
```

```mermaid
flowchart TD
    Start(["首次注册或按需恢复"]) -->|"resources.put(instance) 或 resources.get(h) 触发 cls.restore(locator)"| Active["InTable_Active (_entries[rid] 活跃, closing = False)"]
    Active -->|"await resources.get(h) 本地命中并校验独占借用"| Active
    Active -->|"resources.discard(h) (仅限 locator is None) 记入 attempt_closed_rids, 不触发 dispose"| Popped(["Popped (已从 _entries 真实移除)"])
    Active -->|"await resources.close(h) 或 await store.gc_dispose(...)"| Closing["InTable_Closing (持有 entry.lock, closing = True)"]
    Closing -->|"close(h) 成功 或 gc_dispose 无论成败"| Popped
    Closing -->|"close(h) 中 dispose 抛出异常 (恢复 closing = False)"| Active
```

各操作在该状态机下的精确行为：

所有 `get`、`close`、`discard` 均先核对当前上下文获准访问的 `rid`、不可变身份与终结状态；禁止凭相同 `kind` 猜测访问权限。Attempt 启动时按 `(input_schema, input_snapshot)` 配对扫描，并合并 Workflow 下发的规范状态，建立按 ID 索引的 `exclusive_rids` 与 `terminal_rids`；新分配资源也加入该上下文。

完成访问权限与终结检查后，若本地存在活跃 `entry`，还须核对 `entry.kind == handle.kind` 与第 3.1 节的精确类身份不变量；不匹配时抛 `TypeError`，不得把该实例交给业务。无本地实例时沿用各操作原有的按需恢复或报错规则，不为身份检查额外调用 `restore`。

对仍活跃的输入资源执行 `close` / `discard`，必须持有该 `rid` 的独占调度借用；业务节点通过输入类型的 `exclusive: true` 请求此限制，即使资源本身允许共享读取也适用。当前 Attempt 尚未导出的新资源由其独占拥有。不能仅凭当前 Worker 没有其他使用者，就终结另一个 Worker 仍在使用的共享外部资源。

1. **`await get(handle) -> Any`（缓存命中、按需透明恢复与运行期独占借用检测）**：
   * 若 `rid` 在 `attempt_closed_rids` 或上游传入的 `terminal_rids` 中，立即报已终结资源错误；检查发生在 `restore` 之前，且初始化锁内必须再次检查，不能因恢复等待而绕过终结状态。
   * **情况 A（当前 Worker 本地 `_entries` 命中 `entry`）**：
     * 若 `entry.closing == True`（正处于异步 `dispose` 挂起期间）：抛出 `RuntimeError(f"resource handle is currently closing: {rid}")`，防止并发协程读写半关闭状态的物理对象；
   * **情况 B（当前 Worker 本地 `_entries` 未命中 `rid`）**：
     * 若 `handle.locator is None`：说明该句柄是创建于其他 Worker 或已销毁的纯内存瞬态资源，抛出 `RuntimeError(f"unknown or already closed resource handle: {rid}")`；
     * 若 `handle.locator is not None`：获取该 `rid` 的进程内初始化锁 `async with self._get_init_lock(rid):`，二次检查 `self._entries.get(rid)`；若仍未命中，按 `handle.kind`（`<ModulePath>:<ClassName>`）加载对应的 `@resource` 类 `cls`，调用 `value = await cls.restore(deepcopy(handle.locator))` 恢复物理实例；确认 `type(value) is cls` 后才写入 `self._entries[rid] = ResourceEntry(...)`，并将 `rid` 记入当前 Attempt 的 `rehydrated_rids` 集合（随 Activity 返回值上报给 Workflow 记录进 `resident_queues`）；
   * **运行期独占借用校验**：若 `rid in exclusive_rids`、`entry.exclusive` 为真或条目已有独占借用者，执行以下检查；`close` 与 `discard` 同样适用，不能绕过正在使用该实例的其他 Attempt：
     * 若 `entry.active_attempt_id is not None and entry.active_attempt_id != current_attempt_id`，立即抛出 `RuntimeError(f"concurrent access to exclusive resource handle: {rid}")`（转化为可恢复的 `execution` 异常）；
     * 若该句柄处于独占上下文，将其 `entry.active_attempt_id = current_attempt_id` 绑定至当前 Attempt，直到业务与其所有派生任务排空、清理完成后才释放。共享借用也须记账，终结操作不能关闭其他 Attempt 仍在使用的实例；
   * 返回 `entry.value`。
2. **`discard(handle) -> None`（仅限无 `locator` 的纯内存瞬态资源）**：
   * **禁止对可恢复资源调用 `discard`**：若 `handle.locator is not None`（或本地 `_entries` 条目的 `locator is not None`），立即抛出 `RuntimeError(f"cannot discard reconstructible resource handle with locator: {rid}; use close() instead")`；可恢复资源跨 Worker 存在副本缓存，必须统一通过 `close` 或被动 RC 归零执行 `dispose`，不支持跳过析构钩子；
   * 若 `rid` 在 `attempt_closed_rids` 或 `terminal_rids` 中：幂等返回 `None`；
   * 若 `entry := self._entries.get(rid)` 不存在：抛出 `RuntimeError(f"resource handle is not resident on this worker: {rid}")`；
   * 若 `entry.closing == True`：抛出 `RuntimeError(f"cannot discard resource handle while it is closing: {rid}")`；
   * 执行 `self._entries.pop(rid, None)` 真实移除条目，将 `rid` 记入 `attempt_closed_rids` 与 `discarded_rids`；即使后续业务失败，也须在生命周期元数据中上报，不能仅随成功响应移交。
3. **`await close(handle) -> None` 与 `await gc_dispose(items) -> None` 的重入同步**：
   * 对显式 `await close(handle)`：
     * 若 `rid` 在 `attempt_closed_rids` 或 `terminal_rids` 中：幂等返回；
     * 若 `rid` 不在当前 Worker 的 `self._entries` 中：
       * 若 `handle.locator is not None and handle.persistent`（例如在未调用过 `get` 的新 Worker 节点上直接调用 `await resources.close(input.s3_file)`）：先按 `handle.kind` 加载 `@resource` 类 `cls` 并通过 `cls.restore(deepcopy(handle.locator))` 恢复出 `entry`；
       * 否则抛出 `RuntimeError("resource handle is not resident on this worker")`，不标记关闭。瞬态资源不能在别处关闭；可恢复的非 persistent 资源可先 `get` 建立当前实例，或在原驻留 Worker 执行关闭。显式调用只确认当前实例钩子的结果，其余副本的清理结果由内部清理状态记录。
     * 通过与 `get` 相同的初始化锁取得条目，再获取 `entry.lock`；锁内重查条目身份、终结和借用状态，置 `entry.closing = True` 并执行 `dispose`，防止等待锁的调用继续操作已被摘除的旧条目：
       * 若 `dispose` 成功完成：摘除条目并记入 `attempt_closed_rids` 与 `closed_rids`；
       * 若 `dispose` 抛出异常或被取消：恢复 `entry.closing = False` 并保留条目，再传播原始异常；这只保留后续处置机会，不保证底层对象可再次执行同一操作。
   * 对被动 `await gc_dispose(items)`：
     * 与初始化和显式关闭共享逐 ID 同步机制，在本地使用者排空后才处理该实例。仅当 `rehydrate_for_dispose=True` 且本地未命中时，才为持久化资源按 `kind` 加载 `@resource` 类 `cls` 并调用 `cls.restore(locator)` 恢复临时实例以执行 `value.dispose()`；其余情况若本地存在条目则执行 `dispose`，若本地不存在则返回 `missing`。
     * 在受保护的清理任务中执行钩子，捕获并记录业务析构异常，在 `finally` 中摘除自己处理的条目，返回对应状态（`disposed` / `missing` / `failed`）；若条目已被替换，不可无条件 pop 新条目。取消请求不能使调用方提前放弃等待；预算超时应记录未确认状态，而不是宣称清理成功。初始化锁记录也在无条目、无使用者和等待者后释放，避免长期累积。

#### 3.4.2 为什么提供仅限瞬态资源且无返回值的 `discard(handle) -> None` 而非 `pop(handle) -> Any`？

1. **为什么限制 `discard` 仅用于 `locator is None` 的纯内存瞬态资源？**
   * 在三类资源中，唯有无 `locator` 的纯内存瞬态资源（如数据库事务 `infra.postgres:Tx`）存在“异常时回滚（执行 `dispose`）、正常时手工提交（`await tx.commit(); await tx.close()` 后跳过 `dispose`）”的双路径终结需求，且其物理实例严格绑定单一 Sticky Worker 内存，绝无跨 Worker 缓存副本；
   * 反之，携带 `locator` 的可恢复资源（如 `infra.postgres:Conn`、`infra.s3:TempFile`）可在多个 Worker 上建立含本地流或连接的物理副本。若允许在某 Worker 上 `discard` 跳过 `dispose`，会导致其他 Worker 副本要么不调 `dispose` 泄漏本地流，要么调 `dispose` 误删外部对象。因此对含 `locator` 的资源全面禁用 `discard`，彻底消除 `evict_only` 等跨副本半清理状态。
2. **为什么 `discard` 不返回任何值而非 `pop(handle) -> Any`？**
   * 若提供同时兼具“取出对象”与“注销句柄”的 `pop(handle) -> Any`，用户在编写终结节点（如提交数据库事务的 `commit_tx` 节点）时，会自然写出“先 `tx = resources.pop(input.tx)` 注销句柄、再执行 `await tx.commit()`”的危险序列。一旦 `await tx.commit()` 因网络抖动或锁冲突抛出异常：
     * **节点级重试（`retry`）失效**：由于句柄在第一次 Attempt 的前半段已被 `pop` 从 `ResourceStore` 中永久抹除，第二次重试 Attempt 调用 `resources.pop(input.tx)`（或 `get`）会直接报 `RuntimeError: unknown or already closed resource handle`；
     * **异常展开自动回滚失效**：若重试耗尽进入栈展开（Unwind），由于 `dispose` 已被提前注销，运行时自动触发的 `gc_dispose` 也无法回滚并关闭该连接，直接导致物理连接泄漏。

为此，`ResourceContext` 严格分离 **解引用（`await get`）** 与 **注销（`discard`）** 职责：

* **`await resources.get(handle) -> Any` 是获取底层物理对象的唯一入口**；
* **`resources.discard(handle) -> None` 不返回任何值**，强制引导用户在完成自定义终结动作（如 `await tx.commit()` 和 `await tx.close()`）**成功之后**，才调用 `resources.discard(input.tx)` 将条目从 `_entries` 中移除：

```python
# 仅展示成功路径的顺序；不表示 commit 失败后可对同一事务重试
tx = await resources.get(input.tx)
await tx.commit_and_close()
resources.discard(input.tx)
```

若 `commit()` 抛错，运行时保留条目与清理钩子，但不能恢复事务本身。重试、回滚与提交结果判定取决于底层协议；例如 [asyncpg 的事务实现](https://raw.githubusercontent.com/MagicStack/asyncpg/master/asyncpg/transaction.py)在提交异常后进入 `FAILED` 状态，不能继续对该对象提交或回滚。提交成功而随后关闭连接失败，也不能通过重试该节点再次提交。此类场景应由业务判断提交结果，必要时在外层以新事务重试；析构钩子必须在回滚失败时仍通过 `finally` 尝试关闭连接。

#### 3.4.3 显式关闭（`resources.close`）与被动垃圾回收（`store.gc_dispose`）的异常与生命周期协议

即使引入了可恢复数据库连接与 S3 临时文件，运行时依然严格维持“**显式 `close` 可抛错可重试、被动 `gc_dispose` 视为编程语言级 `noexcept` 析构函数**”的核心法则：

| 维度 | 显式关闭：`await resources.close(handle)` | 被动 GC 回收：`await store.gc_dispose(items)` |
| :--- | :--- | :--- |
| **定位与语义** | 普通命令式业务操作（如显式关闭连接、显式删除临时文件并校验结果） | **编程语言级 `noexcept` 析构函数（如 Rust `Drop` / C++ `~T() noexcept`）** |
| **触发时机** | 用户在 `run` 节点的业务代码中主动调用 | 1. **Attempt 级孤儿回收**：`execute_run` 的 `finally` 中回收未通过 `output` 导出的句柄；2. **栈帧级 RC 归零与残留副本回收**：变量覆写、`kill_vars`、在途异常丢弃、子图退出使 `ref_count == 0` 时，或显式 `close` 后清理其余驻留副本时，通过 `dispose_resources` Activity 触发。 |
| **`dispose` 成功时** | 执行 `self._entries.pop(rid, None)` 从字典中真实移除，并标记 `rid` 已显式关闭。 | 执行 `self._entries.pop(rid, None)` 从字典中真实移除。 |
| **`dispose` 抛异常时** | **恢复 `entry.closing = False` 并保留条目在 `ResourceStore._entries` 中**，同时将异常原样向上抛出给当前 `run` 节点。 | **在 `finally` 中无条件执行 `self._entries.pop(rid, None)` 从字典中真实移除**；Worker 捕获异常并记录结构化 `ERROR` 日志（`logger.exception`），**绝不向外抛出异常（`noexcept`）**。 |
| **Worker 不可达时的回退行为** | 按节点的重试与借用收敛协议处理，不把超时当作已停止。 | 若 `persistent == False`：不重建，保留未确认副本的清理责任；若 `persistent == True` 且 `not external_disposed`：回退共享队列通过 `cls.restore(locator)` 恢复并清理外部对象，其余本地副本仍需摘除。 |

**为什么即使包含 S3 临时文件清理，被动回收 `store.gc_dispose` 依然必须严格定位为 `noexcept` 析构函数？**

1. **绝不击穿静态异常分析与无辜纯算子**：在 Axcutor 中，栈帧级 `ref_count == 0` 常由下游节点的变量覆写或边上的 `kill_vars`（含挂载在零可恢复异常的 `builtin:unwrap`、`builtin:assign` 边上的 `kill_vars`）以及子图到达 `-> end` 弹栈触发。在编译期静态分析（Pass 4）中，这些位置均为 **零可恢复异常点**——若被动析构向外抛错，会在无 `catch` 分支的位置直接击穿静态异常完备性不变量；
2. **绝不掩盖主异常（Double-Fault 防护）**：当节点因 `timeout`、`contract` 违约、业务 `execution` 报错或 `CancelledError` 取消而触发 Attempt 级孤儿回收或栈帧展开（Unwind）时，若清理 S3 临时文件或回滚连接顺带抛出的次生网络异常向外逃逸，将覆盖并篡改原始主异常类别，破坏外层 `catch` 分支路由与错误诊断；
3. **职责边界清晰**：若某次外部文件删除的成败本身属于核心业务契约（失败必须重试或走特定业务补偿分支），用户应在配置了 `retry` / `catch` 的业务节点中显式调用 `await resources.close(h)`；一旦交由作用域结束的 `gc_dispose` 兜底，它就是纯粹的资源析构器（Finalizer / Drop）。

#### 3.4.4 导出句柄存活契约校验与单次 Activity Attempt 级孤儿防漏机制（Attempt-Scoped Orphan Protection）

在 `RunActivities.execute_run` 中，存在两个必须在 Activity 边界闭环的关键问题：
1. **禁止导出已关闭/已丢弃的悬垂句柄（Dangling Handle Export Check）**：如果某个 `run` 节点在内部已经对句柄 `h` 调用了 `await resources.close(h)` 或 `resources.discard(h)`，随后却仍将该失效的 `h` 放入 `Output` 返回给下游，必须当场作为 **`contract` 异常（`ContractError`）** 拦截，严禁悬垂句柄流入工作流逻辑堆；
2. **孤儿句柄防漏**：如果某个 `run` 节点在代码前半段调用了 `await resources.put(instance)` 开启了事务或创建了 S3 临时文件，但在返回前抛出了异常、超时、被取消，或者返回的 `Output` 未通过 `validate_run_contract` 校验（又或者该句柄未被选中的分支导出），此时该句柄尚未进入 Workflow 的逻辑堆 `self._registry`，必须在当前 Attempt 结束时立即就地调用 `dispose` 回收。

为此，`RunActivities.execute_run` 执行以下闭环流程：

整个流程受同一 Attempt 的 `try ... finally` 托管；预登记也受期限、心跳与取消守护约束，任何退出路径均须恢复上下文。

1. 在每次 `execute_run` 启动时，初始化当前 Attempt 的 `allocated_rids`、`rehydrated_rids`、`resident_rids`、`closed_rids`、`discarded_rids` 与 `attempt_closed_rids`，并从输入 schema、快照和 Workflow 内部上下文建立 `exclusive_rids`、`terminal_rids`。追踪集按资源 ID 索引，序列化时使用固定顺序；绑定 `ContextVar` 必须发生在创建业务 Task 之前。
   * 驻留责任在业务执行与任何资源初始化锁之前登记：目标 Sticky 队列已知时，由 Workflow 在调度前预登记相关可恢复输入；共享队列认领者尚未知时，由 Activity 启动阶段向**直接调用它的确切 Workflow Run** 发送含 `worker_queue` 与 `reserved_rids` 的 `resource_update`，并在有限期限内等待 Temporal Server 持久化接收。所用 Run 标识来自 Activity 调用上下文，不复用业务 ABI 中表示包名的 `workflow_id`。
   * Signal 的成功返回只表示 Server 接收并记录了消息，不表示业务处理函数已完成（参见 [Temporal Python Signal 文档](https://docs.temporal.io/develop/python/workflows/message-passing#send-a-signal)）。登记不等待父或祖父 Workflow 的应用层 ACK、不轮询 Query；RPC 失败或中止时不开始构造副本。响应丢失时可用相同来源和序号重发。接收方的内部处理函数只同步合并状态，不等待 Activity / Child 结果；主调用边界在释放借用前应用已收到的更新，Child 在退出前将累计责任持久化交给父 Run。
   * 登记后才能构造输入副本；正常及业务失败响应均更新实际驻留情况，已登记副本随所有者的 RC 生命周期保留，不能因本次业务失败删除仍被父作用域引用的外部实体。登记后未构造或构造失败时，清理可在该队列获得 `missing` 确认；这类预登记是保守的清理责任，不声明物理实例已经存在。
2. 每次调用 `resources.put(instance)` 时，将新生成的 `rid` 记入 `allocated_rids`；
3. 执行器返回后，按第 3.1.1 节汇总并规范化资源独占状态；使用 `validated = loads(axcutor_core.validate_run_contract(...))` 取得校验且投影后的业务结果字典，不能直接索引 JSON 字符串：
   * 对照 `output_schema` 扫描 `validated["output"]`，提取被选中分支实际导出的句柄集合 `exported_rids`；
   * **存活契约校验**：逐一检查 `exported_rids` 中的每个 `rid`：
     * 若 `rid` 在 `attempt_closed_rids` 或 `terminal_rids` 中，立即抛出 `ContractError(f"cannot export closed or discarded resource handle: {rid}")`（归类为可恢复的 **`contract`** 异常）；
     * 若 `rid in allocated_rids`（或为纯内存瞬态句柄 `locator is None`），必须仍存活于 `store._entries` 中（且 `not entry.closing`），否则同样抛出 `ContractError`；
     * 若本地存在该 `rid` 的活跃条目，还须核对 `entry.kind == handle.kind == expected_kind` 与 `type(entry.value) is resolve_resource_class(expected_kind)`，其中 `expected_kind` 来自当前输出 schema 的字段位置；不匹配时抛出 `ContractError`。对合法透传且本地未驻留的可恢复句柄，只校验信封与规范身份，不为导出检查额外恢复实例；
4. 在 `finally` 中先 `await cancel_and_drain(tasks)`，再检查 `activity.is_cancelled()` 并同步更新 `signal`，随后判定孤儿集合；资源借用锁在清理收敛之后释放：
   * 正常通过全部校验且未中止时，清理 `allocated_rids - exported_rids`；报错、超时、取消或契约失败时，清理全部 `allocated_rids`。两条路径均持有清理任务并循环 shield 等待，按第 3.3 节处理重复取消和有限预算。
   * 业务清理结束后才形成生命周期元数据，且不可因后续业务异常而回滚已经完成的 `close` / `discard`。最后释放本地借用并 reset `ContextVar`，已结束上下文不得供游离 Task 继续分配资源。
   * 内部资源载荷统一为 `{"updates": [...], "transferred": [...]}`。每个 update 包含来源 Run、来源调用标识、单调序号、`worker_queue`、`reserved_rids`、`resident_rids`、`rehydrated_rids`、`closed_rids`、`discarded_rids`、`exclusive_rids` 与排空确认；预登记尚不确认排空，最终更新才可确认。Activity 来源调用标识包含 Activity ID 和 Attempt。每个 transferred 项包含 `resource_id`、`kind`、原始 `base_queue`、完整 `resident_queues`、`locator`、`persistent`、`exclusive`，不传输局部引用计数。
   * 成功的内部 Activity 响应为 `{"result": validated, "resources": resources_payload}`，包含本次 update 及实际导出资源的移交描述。这些字段由 `RunActivities` 构造，既不由业务执行器提供，也不进入 `validate_run_contract` 的业务白名单投影。Workflow 先消费元数据、接管并保活返回句柄，再对 `result` 做业务校验与写回。
   * 业务失败时，在原异常的 details 中附加同一内部元数据，保持原有 `execution` / `contract` / `timeout` 分类。取消或 Server 超时可能使 Activity 完成响应无法送达；此时用内部 `resource_update` 信号交回生命周期变化及排空确认，Workflow 按上述来源与序号去重，在确认前不释放相应借用。回传也失败时保留未确认状态，不能擅自按已关闭、已移交或已停止处理。
   * Workflow 的正常内部结果使用同一 `result` / `resources` 外形，携带其最终导出资源的完整移交描述及借入资源累计更新；失败或取消路径按第 3.3 节先交回内部更新。Host 解包后，将业务 `{branch, output}` 保留在 `Execution.result`，将移交与清理信息作为独立的 `Execution.resources` 提供给调用方；宿主服务、HTTP schema 与 CLI 展示同步更新。业务执行器 ABI 仍为 `{branch, output}`，资源元数据不得混入图变量或被业务输出投影裁剪。
5. **Activity 成功返回前的进程级硬崩溃（Crash / `kill -9` / 掉电）边界说明**：
   * 当 `execute_run` 内部刚调用完 `resources.put(instance)` 但尚未向 Temporal Server 返回 `Output` 时，若 Worker 进程发生直接硬崩溃（进程级 `finally` 无法执行，且句柄尚未移交至 Workflow 逻辑堆 `self._registry`）：
     * 对 **数据库事务与连接**：数据库在检测到连接断开后释放连接并回滚未提交事务；检测并非瞬时，已提交或提交结果未知的副作用不能由资源回收撤销。
     * 对 **外部持久化临时资源**：locator 必须区分 Attempt / 资源代次，或携带精确的外部对象版本；清理只能删除本代对象。禁止旧 Attempt 的迟到清理删除新 Attempt 覆写到同一 Key 的对象。示例可使用资源 ID、Activity Attempt 标识或对象版本作为路径的一部分；在临时前缀配置 **Bucket Lifecycle TTL**，兜底未移交与未确认删除的对象。

---

## 四、 `builtin`（RFC-0012）与 `module`（RFC-0014 / RFC-0015）执行器的强类型 `Handle`、`@resource` 与 `resources` 统一规范

结合 [`RFC-0012`](./0012-run-executor-and-builtin-python.md)、[`RFC-0014`](./0014-module-python-executor.md) 与 [`RFC-0015`](./0015-unified-package-and-import-system.md)，Axcutor 为内联 `builtin` 脚本与模块化 `module` 包提供 **100% 同构** 的 `Handle` 类型契约、`@resource` 资源类导入与 `resources` 访问方式：

### 4.1 基于 `ContextVar` 的全局 `axcutor_runtime.resources` 代理与 `@resource` 导出

在 [`RFC-0014`](./0014-module-python-executor.md) 中，`executor: "module"` 节点的业务实现模块（如 `order_flow/charge_card.py`）是标准 Python 模块文件，其入口函数签名固定为 `async def execute(input: Input) -> ReturnType:`，不经过 `builtin` 执行器的 `exec()` 命名空间注入。

为此，`axcutor_runtime` 在顶层公开导出 `Handle`、`resource` 装饰器与基于 `contextvars.ContextVar` 的上下文代理对象 `resources`：

```python
from axcutor_runtime import Handle, resource, resources
```

* 在 `RunActivities.execute_run` 启动每次 Attempt 时，通过 `_current_resource_context.set(attempt_resource_ctx)` 将当前 Attempt 专属的 `ResourceContext` 绑定到当前异步任务上下文中，并在 `finally` 中 `reset`；
* **在 `module` 执行器中**：业务模块直接通过 `from axcutor_runtime import resources` 调用 `await resources.get(h)`、`await resources.put(instance)`、`await resources.close(h)` 与 `resources.discard(h)`，并通过标准 Python `import` 导入共享包中的 `@resource` 类；
* **在 `builtin` 执行器中**：`_PRELUDE` 默认将 `resources`、`resource` 与 `Handle` 注入模块顶层作用域，用户既可直接访问 `resources`，也可显式 `import` 已挂载包中的 `@resource` 类。

### 4.2 契约模板（Contract Stub）中的强类型 `Handle` 代码生成与 SHA-256 指纹

在 `builtin.synthesize_module`（RFC-0012）与 `synthesize_contract_module`（RFC-0014 生成 `<node>_contract.py` / `_contract.py`）中，共享同一套 `Handle` 模型定义与类型映射规则：

1. **内置 `Handle` 泛型模型与校验器**：
   ```python
   _T = _ax_typing.TypeVar("_T")

   if _ax_typing.TYPE_CHECKING:
       # 与 resources.put/get 使用同一个公共泛型类型，保持静态类型身份一致。
       from axcutor_runtime import Handle as Handle
   else:
       class Handle(_ax_pydantic.BaseModel, _ax_typing.Generic[_T]):
           model_config = _ax_pydantic.ConfigDict(
               extra="forbid", strict=True, frozen=True, populate_by_name=True
           )
           handle_id: StrictStr = _ax_pydantic.Field(alias="$handle")
           kind: StrictStr
           worker_queue: StrictStr
           exclusive: StrictBool = False
           locator: _ax_typing.Any = None
           persistent: StrictBool = False
   ```
   * 泛型参数 `_T` 表示 `@resource` 资源类，使用不变泛型（invariant），只承担静态类型标记；信封中的 `kind` 始终是字符串，其预期值由 DSL schema 提供。
   * 此模型使用固定字段，不继承为任意 DSL 属性名设计的动态别名模型。`_validate_handle(expected_kind: str, expected_exclusive: bool)` 校验完整信封、`actual.kind == expected_kind` 精确相等与独占性单调规则；同时复用第 2.4 节的非空 ID、队列格式、locator 与 persistent 约束，不能只校验 kind。
   * 不同契约模块中的运行期 `Handle` 类身份可以不同；转换时按固定字段提取并重新校验，保留实际 kind、独占性和持久化标志。静态检查统一使用 `axcutor_runtime.Handle[T]`，使契约字段与 `resources.put(value) -> Handle[T]`、`resources.get(handle) -> T` 的签名兼容。资源身份与规范状态仍由 Activity / Workflow 边界核对，Pydantic 冻结并不是业务代码不可伪造的安全边界。
2. **输入/输出类型映射与 `_export` 还原**：
   * 当输入或输出 schema 的字段为 `handle` 时，拆分 `kind: <ModulePath>:<ClassName>`，在模块顶层的 `if _ax_typing.TYPE_CHECKING:` 块中生成 `from <ModulePath> import <ClassName> as _ax_res_<i>`，字段注解生成为 `Handle[_ax_res_<i>]`，并附加携带原始 `kind` 字符串与 `exclusive` 的校验器。同一契约模块按 schema 首次出现顺序为不同 `kind` 分配别名并复用；嵌套容器递归应用此规则，分支契约通过字段选择复用相同输出 schema。
   * 运行期在对应的 `else` 块中将资源类型别名绑定为 `_ax_typing.Any`，使 Pydantic 能直接解析 `Handle[Any]`，无需导入或解析业务资源类。预期 `kind` 与 `exclusive` 仍由字段校验器从 DSL schema 获取，保持契约模块运行期零业务导入与完整信封校验。
     ```python
     # stage_data_contract.py 的相关生成片段；Handle 与模型基类沿用契约前导定义。
     if _ax_typing.TYPE_CHECKING:
         from infra.resources import PostgresConn as _ax_res_0
         from infra.resources import S3TempFile as _ax_res_1
     else:
         # 运行期只校验句柄信封，不加载业务模块或资源实例类型。
         _ax_res_0 = _ax_typing.Any
         _ax_res_1 = _ax_typing.Any

     class Input(_ax_FrozenModel):
         batch_id: StrictStr
         db_conn: _ax_typing.Annotated[
             Handle[_ax_res_0],
             _ax_pydantic.BeforeValidator(
                 _validate_handle("infra.resources:PostgresConn", True)
             ),
         ]

     class Output(_ax_StrictModel):
         staged_file: _ax_typing.Annotated[
             Handle[_ax_res_1],
             _ax_pydantic.BeforeValidator(
                 _validate_handle("infra.resources:S3TempFile", False)
             ),
         ]
         row_count: StrictInt
     ```
   * Python IDE / Mypy 因而将 `Input.db_conn` 与 `Output.staged_file` 分别识别为 `Handle[PostgresConn]` 与 `Handle[S3TempFile]`；`await resources.get(input.db_conn)` 推导为 `PostgresConn`，`await resources.put(s3_file)` 推导为 `Handle[S3TempFile]`，可直接传入 `Output(staged_file=...)`。
   * **DSL 与 Python 两侧的资源类静态一致性（必须）**：对每个 `kind: <ModulePath>:<ClassName>`，生成的契约类型 `Handle[C]` 与 Python 业务侧传入、返回的 `Handle[T]` 必须使用同一个类符号（`C` 与 `T` 解析到同一个类定义）。导入别名不改变类身份；来自不同模块的同名类、仅结构相同的类不能互换。这项联合静态检查由 Axcutor 生成契约、Mypy 解析 Python 类符号并检查类型流共同完成，保持 `axcutor-core` 编译与契约模块运行期零业务导入。
   * 静态检查环境必须采用与目标 Worker 一致的 `module_mounts` 根包映射与资源包版本；非恒等挂载时，`TYPE_CHECKING` 导入目标按第 3.1 节映射为物理 Python 模块，信封与校验器仍保留逻辑 `kind`。资源类导入必须可静态解析为具体类，缺失导入或静态类型退化为 `Any` 不视为通过一致性检查；上例 `else` 中的 `Any` 绑定仅用于运行期模型解析。运行期类对象身份仍按第 3.1 节的 `resolve_resource_class(kind) is cls` 校验。
   * **DSL 与 Python 两侧的资源类运行期一致性（必须）**：Activity 按 schema 校验 `handle.kind == expected_kind`，Worker 的资源存取与恢复路径再保证 `type(value) is resolve_resource_class(expected_kind)`，共同建立 DSL 声明与实际 Python 对象的精确类对应关系。物理条目与导出边界分别按第 3.4.1、3.4.4 节检查；无本地实例的透传句柄延迟至实际解引用或恢复时核验。此检查使用 schema 与资源表中的规范 `kind`，不依赖已在运行期擦除的泛型参数，也不要求契约模块导入业务类。
   * `_export` 在通用 `BaseModel` 分支之前处理各契约模块的 `Handle`，还原 `"$handle"`、实际 `kind` 和 `worker_queue`；`exclusive=True` 时输出该字段，locator 非空时输出其 JSON 值，且**仅当实际 `persistent is True` 时**输出 `"persistent": True`。不能由 locator 是否存在推导 persistent；显式的 `null` / `false` 与省略字段在校验上等价。
   * 在 RFC-0014 的 `compute_contract_sha256` 中，归一化后的 `{"type": "handle", "kind": "...", "exclusive": true}` 直接参与节点契约的 SHA-256 指纹计算（`__axcutor_contract_sha256__`），任何对 `kind` 或 `exclusive` 的修改均会在运行期触发契约漂移检测（Schema Drift Check）；静态导入别名及其编号属于生成细节，不参与指纹。

### 4.3 `@resource` Python 类（`locator` / `restore` / `dispose`）在多工作流包间共享的完整代码示例

```python
# 1. 共享基础设施包中的 Python 资源类定义（workflows/infra/resources.py）
from typing import Any, TypedDict
import asyncpg
from axcutor_runtime import resource


@resource
class PostgresTx:
    """纯内存瞬态数据库事务（对应 kind: infra.resources:PostgresTx，不定义 locator/restore）。"""

    def __init__(self, conn: asyncpg.Connection, tx: asyncpg.transaction.Transaction) -> None:
        self.conn = conn
        self.tx = tx

    async def commit_and_close(self) -> None:
        try:
            await self.tx.commit()
        finally:
            await self.conn.close()

    async def dispose(self) -> None:
        try:
            await self.tx.rollback()
        finally:
            await self.conn.close()


class DsnLocator(TypedDict):
    dsn: str


@resource
class PostgresConn:
    """可跨 Worker 恢复的数据库连接（对应 kind: infra.resources:PostgresConn）。"""

    def __init__(self, dsn: str, conn: asyncpg.Connection) -> None:
        self.dsn = dsn
        self.conn = conn

    def locator(self) -> DsnLocator:
        return {"dsn": self.dsn}

    @classmethod
    async def restore(cls, locator: DsnLocator) -> "PostgresConn":
        conn = await asyncpg.connect(dsn=locator["dsn"])
        return cls(locator["dsn"], conn)

    async def dispose(self) -> None:
        await self.conn.close()


class S3Locator(TypedDict):
    bucket: str
    key: str


@resource(persistent=True)
class S3TempFile:
    """可跨 Worker 恢复且支持保底清理的 S3 临时文件（对应 kind: infra.resources:S3TempFile）。"""

    def __init__(self, bucket: str, key: str, stream: Any) -> None:
        self.bucket = bucket
        self.key = key
        self.stream = stream

    def locator(self) -> S3Locator:
        return {"bucket": self.bucket, "key": self.key}

    @classmethod
    async def restore(cls, locator: S3Locator) -> "S3TempFile":
        return await cls.open(bucket=locator["bucket"], key=locator["key"])

    async def dispose(self) -> None:
        try:
            await self.delete_object_idempotent()
        finally:
            await self.close_local_stream()
```

```yaml
# 2. 工作流包定义（workflows/etl/stage/__axcutor__.yaml）
# 任何工作流只需声明相同的 kind: infra.resources:<ClassName> 即可共享资源类型
input:
  batch_id: { type: string }
  db_conn: { type: handle, kind: infra.resources:PostgresConn, exclusive: true }
output:
  staged_file: { type: handle, kind: infra.resources:S3TempFile }
  row_count: { type: integer }

graph:
  nodes:
    stage_data:
      input: [batch_id, db_conn]
      output:
        staged_file: { type: handle, kind: infra.resources:S3TempFile }
        row_count: { type: integer }
      run:
        executor: module
        content: .stage_data
  edges:
    - start -> stage_data
    - stage_data -> end
```

```python
# 3. 业务节点模块示例（workflows/etl/stage/stage_data.py）
from uuid import uuid4

from axcutor_runtime import resources
from infra.resources import PostgresConn, S3TempFile
from .stage_data_contract import Input, Output, ReturnType


async def execute(input: Input) -> ReturnType:
    # 1. 解引用可恢复数据库连接（若原 Worker 宕机迁移至此，自动按 infra.resources:PostgresConn 调用 PostgresConn.restore 恢复）
    pg: PostgresConn = await resources.get(input.db_conn)
    rows = await pg.conn.fetch("SELECT * FROM orders WHERE batch_id = $1", input.batch_id)

    # 2. 写入 S3 临时文件并直接传入实例 s3_file 注册（自动提取 s3_file.locator() 与 kind="infra.resources:S3TempFile"）
    # 每代使用独立对象 Key，避免旧 Attempt 的迟到清理删除重试生成的对象
    s3_file = await S3TempFile.upload(
        bucket="etl-scratch",
        key=f"batches/{input.batch_id}/{uuid4().hex}.parquet",
        rows=rows,
    )
    temp_handle = await resources.put(s3_file)  # 推导为 Handle[S3TempFile]，与 Output.staged_file 一致

    return Output(staged_file=temp_handle, row_count=len(rows))
```

---

## 五、 变更影响模块清单

| 模块路径 | 变更内容摘要 |
| :--- | :--- |
| `crates/axcutor-core/src/ast/typespec.rs` | 新增 `TypeKind::Handle { kind: String, exclusive: bool }`；校验 `kind` 的 `<ModulePath>:<ClassName>` 文法（`package_path` + `:` + `package_segment`）；实现 `kind` 精确相等与 `exclusive` 单调提升子类型规则；递归字面量校验及支持 `exclusive` / `locator` / `persistent` 的运行期信封校验。 |
| `crates/axcutor-core/src/compiler/check.rs` | 校验 `handle.kind` 中 `<ModulePath>` 与 `<ClassName>` 的 `_` 私有可见性（`visibility.private_resource`）；将编译期常量字面量校验切换为 `accepts_literal`（拒绝非 `null` 的 `handle` 字面量）；禁止对 `handle` 做字段路径投影；禁止 `for_each (concurrency > 1)` 的节点 `body` 读取外层 `exclusive` 句柄（允许 `item` 本身为独占句柄）。 |
| `crates/axcutor-core/src/compiler/builtins.rs` | 将 `equality` 与 `object_merge` 中的编译期常量校验切换为 `accepts_literal`；保留 `compare` 的句柄 `eq`/`ne` 类型检查，禁止有序比较。经 `argument()` 校验的容器字面量同样递归执行字面量规则。 |
| `crates/axcutor-core/src/kernel/mod.rs` | 输入与输出校验调用 `accepts_runtime`；`compare` 的运行期相等性按操作数类型递归处理句柄 ID，不改写普通映射及编译期字面量相等性。 |
| `crates/axcutor-core/src/compiler/flow.rs` | 在 Fork-Join 并发组校验中，禁止多个并行分支同时借用含有 `exclusive: true` 句柄的同一变量（支持 `use` 节点字段路径投影后的精确类型判定）。 |
| `python/axcutor_runtime/src/axcutor_runtime/resources.py` | **新增**：`@resource(persistent=...)` 类装饰器（`locator` / `restore` / `dispose` 三要素校验）、基于 `module_mounts` 的 `<ModulePath>:<ClassName>` 资源类双向解析器、`Handle`、条目与 `ResourceStore`；单实例 `resources.put(instance, exclusive=...)`、统一 `get`/`close`/`discard`（限制 `discard` 仅用于无 `locator` 瞬态资源）的身份、终结和借用检查；支持同步或异步方法、逐 ID 初始化及析构同步、带结果状态（`disposed`/`missing`/`failed`）的被动清理、Attempt 上下文与 `resources` 代理。 |
| `python/axcutor_runtime/src/axcutor_runtime/builtin.py` | `_PRELUDE`、`synthesize_module` 与契约模板生成（RFC-0014 `synthesize_contract_module`）统一支持含 `locator`/`persistent` 的 `Handle`（精确匹配 `kind`）、公共 `Handle[T]` 静态类型身份、`TYPE_CHECKING` 资源类别名导入与运行期 `Any` 绑定、`_ax_ResourceContext` 静态声明、跨命名空间 `Handle` 转换与 `_export` 序列化；在 `execute_builtin` 与 `execute_module` 中共享 `ContextVar` 绑定的 `resources` 与 `resource`。 |
| `python/axcutor_runtime/src/axcutor_runtime/activities.py` | 集成资源上下文（共享 `module_mounts`）、独占规范化与导出存活检查；成功及失败传递独立生命周期元数据；取消、超时使用内部更新交回状态与排空确认；有界等待孤儿清理，新增返回逐项状态的 `dispose_resources`。 |
| `python/axcutor_runtime/src/axcutor_runtime/workflow.py` | 按类型扫描、确定性 RC 与保活槽、按 rid 借用及终结状态（含 `external_disposed` 追踪）；Activity / Child 元数据接管与幂等内部更新；句柄节点由 Workflow 重试；按 executor 池选 Sticky 队列；有界被动清理、保留未确认驻留责任及清理诊断。 |
| `python/axcutor_runtime/src/axcutor_runtime/worker.py` | 复合 Worker 的双队列运行、失败回收与 `shutdown()` 接口；主 Worker 与 Sticky Worker 共享 `ResourceStore` 与 `module_mounts`，在 Activity 排空后关闭 Store。 |
| `python/axcutor_host/src/axcutor_host/{service,http,cli}.py` | 解包内部 Workflow 结果；保持业务 `Execution.result`，通过 `Execution.resources` 交付资源所有权与清理信息，同步更新类型、HTTP schema 和展示。 |
