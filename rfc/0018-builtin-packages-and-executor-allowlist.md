# RFC-0018: 按根挂载编译单元（`MountUnit`）、`__ax_resources__.yaml` 资源声明与编译期执行器白名单机制

* **状态**：Proposed
* **所属分类**：按根命名空间挂载编译单元、`__ax_resources__.yaml` 静态资源包声明与编译期执行器白名单控制（Root-Mounted Compilation Units, Static Resource Package Declaration & Compile-Time Executor Allowlist）
* **关联提案**：
  * [`RFC-0012: 结构化 run 执行器分派机制、移除自然语言变量正则提取与 builtin 可信 Python 执行器规范`](./0012-run-executor-and-builtin-python.md)
  * [`RFC-0013: 不透明句柄类型与跨 Activity / Workflow 资源生命周期`](./0013-opaque-handle-type-and-resource-registry.md)
  * [`RFC-0014: 基于预置 Python 模块引用的 module 执行器、__axcutor__.yaml 包同构布局与契约模板 SHA 校验规范`](./0014-module-python-executor.md)
  * [`RFC-0015: Axcutor 统一包模型与点分模块寻址（Package & Import System）规范`](./0015-unified-package-and-import-system.md)
  * [`RFC-0017: 协作式挂起执行器 signal、Temporal Update 恢复协议与宿主 :signal 接口规范`](./0017-cooperative-signal-executor.md)

---

## 一、目标与问题边界

在 [RFC-0013](./0013-opaque-handle-type-and-resource-registry.md) 与 [RFC-0015](./0015-unified-package-and-import-system.md) 的基础上，当宿主基于 Axcutor 提供预置工作流节点库与受管资源类型，或将 Axcutor 裁剪为受限的可控发行版时，需要解决以下三个核心问题：

1. **独立的 `__ax_resources__.yaml` 资源包静态头文件声明**：在 [RFC-0013](./0013-opaque-handle-type-and-resource-registry.md) 中，编译器仅校验 `handle` `kind` 的词法格式 `<ModulePath>:<PublicClassName>` 与私有段可见性，无法在不导入 Python 的前提下静态核对资源类是否真实存在；同时 `__axcutor__.yaml` 唯一对应可调用的工作流定义，不宜混入非工作流的资源元数据。为此引入专用的 **`__ax_resources__.yaml`** 头文件声明包内公开受管资源类。
2. **确立按顶层根命名空间（Root）组织的统一挂载单元 `MountUnit`**：
   * 所有包（无论是宿主预置的标准库、扩展插件包，还是外部动态提交的纯编排包及匿名入口 `"__main__"`）在 [`CompileRequest`](../crates/axcutor-core/src/compiler/mod.rs) 中均统一表示为按顶层根名组织的 `mounts: Ordered<MountUnit>`；
   * 每个 `MountUnit` 仅包含三个正交字段：
     * `packages: Ordered<SourceDocument>`：该根下的 `__axcutor__.yaml` 工作流文档；
     * `resources: Ordered<SourceDocument>`：该根下的 `__ax_resources__.yaml` 资源声明头文件；
     * `allowed_executors: Option<Vec<String>>`：该根下工作流 `run` 节点允许使用的执行器白名单（`None` 表示不限制，`Some(list)` 表示仅允许列表中的执行器，如 `Some(vec!["signal"])` 或 `Some(vec![])`）。
3. **仅通过控制 `CompileRequest` 即可完成受控发行版裁剪，Runtime 与 Worker 零冗余侵入**：
   * 宿主在构造 `CompileRequest` 时，只需在预置根（如 `"std"`）中传入其 `packages` 与 `resources`，而在用户根（如 `"app"` 或 `"__main__"`）中不传 `resources` 并设置 `allowed_executors`（如 `Some(vec!["signal"])`）：
     * 用户工作流若使用未在 `resources` 中声明的 `handle` `kind`，编译期直接因不在全局资源符号表 $\Sigma_{\text{res}}$ 中而报 **`type.unknown_resource`**；
     * 用户工作流若使用不在该根 `allowed_executors` 中的执行器（如内联 Python `"builtin"` 或 `"module"`），编译期直接报 **`schema.executor_not_allowed`**；
   * 前端 `axcutor-wasm` 与后端 `compile_bundle` 共享完全一致的 `CompileRequest` 结构，运行时 Worker 无需任何白名单改动。

---

## 二、核心设计决策

### 2.1 `__ax_resources__.yaml` 资源包声明规范（对 RFC-0013 的静态声明扩展）

为保持 `__axcutor__.yaml`（唯一对应可调用工作流包）的语法正交性，同时使编译器在不导入 Python 代码的前提下静态验证所有 `handle` `kind` 的存在性，引入专用的资源包声明文件 **`__ax_resources__.yaml`**：

1. **目录与包路径同构映射**：
   * 在已挂载的根包目录树下，一个包含 `__ax_resources__.yaml` 与 `__init__.py` 的文件夹定义一个**资源包（Resource Package）**，其逻辑路径 `ModulePath` 为从挂载根名起始的点分路径（例如 `infra/postgres/__ax_resources__.yaml` 对应 `infra.postgres`）；
   * 同一文件夹可仅包含 `__ax_resources__.yaml`（纯资源包），也可同时包含 `__axcutor__.yaml`（兼具同路径工作流与资源声明）；
   * `"__ax_resources__"` 与 `"__axcutor__"` 同为系统保留标识符，禁止作为 `package_path` 或 `handle` `kind` `<ModulePath>` 中的包路径段使用；
   * 对应的 Python `@resource` 类由该资源包的 `<ModulePath>/__init__.py` 公开导出（可直接定义于 `<ModulePath>/__init__.py`，或定义于该资源包目录下的单文件子模块如 `<ModulePath>._tx` / `<ModulePath>.tx` 中并由 `<ModulePath>/__init__.py` 重导出；禁止跨资源包重导出）：正向按 `<ModulePath>` 从 `__init__.py` 导入公开类 `<ClassName>`，反向通过实例定义模块的所属包 `sys.modules[cls.__module__].__package__` 推导唯一逻辑 `kind`（`<ModulePath>:<ClassName>`）。
2. **`__ax_resources__.yaml` 闭合语法**：
   ```yaml
   # infra/postgres/__ax_resources__.yaml
   resources:
     Tx:
       description: "PostgreSQL 数据库事务句柄"
     Conn:
   ```
   * 顶层映射必须且仅能包含 `resources` 键（`closed(m, &["resources"], ...)`）；
   * `resources` 必须为非空映射对象，每个键 `<ClassName>` 必须为不以 `_` 开头的公开 `package_segment`；
   * 每个资源项的值可为 `null`（即简写 `Conn:`）或仅包含可选 `description: string` 字段的闭合映射对象；该声明在当前包路径 `<ModulePath>` 下静态注册资源类型 `<ModulePath>:<ClassName>`。
3. **编译期静态存在性检查（`type.unknown_resource`）**：
   * `TypeSpec::parse` 保持无状态纯词法校验（仅验证 `kind` 满足 `<ModulePath>:<PublicClassName>` 且不含 `"__axcutor__"` 或 `"__ax_resources__"` 保留段）；
   * 编译器在阶段 1（`schema.rs`）解析所有挂载根下传入的 `__ax_resources__.yaml` 文档集合，构建全局静态资源符号表 $\Sigma_{\text{res}}$；
   * 当工作流或节点声明 `type: handle, kind: <ModulePath>:<ClassName>` 时，编译器除检查 `workflow_visible(caller, <ModulePath>)` 私有段可见性（违者报 `visibility.private_resource`）外，还强制核对 `<ModulePath>:<ClassName> \in \Sigma_{\text{res}}$；若未在任何已加载的 `__ax_resources__.yaml` 中声明，直接报告静态诊断错误 **`type.unknown_resource`**。

---

### 2.2 按根命名空间的挂载单元 `MountUnit`（`axcutor-core`）

将 [`CompileRequest`](../crates/axcutor-core/src/compiler/mod.rs) 收敛为按顶层根命名空间（Root）组织的 `mounts: Ordered<MountUnit>`：

```rust
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CompileRequest {
    pub entry: String,
    pub mounts: Ordered<MountUnit>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MountUnit {
    #[serde(default)]
    pub packages: Ordered<SourceDocument>,
    #[serde(default)]
    pub resources: Ordered<SourceDocument>,
    #[serde(default)]
    pub allowed_executors: Option<Vec<String>>,
}
```

#### 2.2.1 根挂载键约束与天然防遮蔽性质

1. **挂载根键（Mount Root Key）词法约束**：
   * `request.mounts` 的每个键 `root` 必须为 `"__main__"`，或为合法的公开根包段（满足 `package_segment(root) && !root.starts_with('_') && root != "__axcutor__" && root != "__ax_resources__"`）；
   * 若 `root` 非法，编译器在 `Address::root(root)` 处报告静态诊断错误 **`schema.mount_root`**（`"mount root must be __main__ or a public package segment"`）。
2. **挂载单元内部包路径归属不变式（Root Ownership Invariant）**：
   * 当 `root == "__main__"` 时：
     * `mount.packages` 中的键仅允许为 `"__main__"`；
     * `mount.resources` 必须为空（匿名根不支持导出可寻址的 `<ModulePath>:<ClassName>`），若非空则对其中每个键报告 **`schema.package_path`**。
   * 当 `root != "__main__"` 时：
     * `mount.packages` 与 `mount.resources` 中的每个键 `id` 均必须满足 `package_path(id)`，且其顶层根段必须恒等于当前挂载根键：$\text{root}(id) = \texttt{id.split('.').next()} == \texttt{Some(root)}$；
     * 若违反，在 `Address::root(id)` 处报告静态诊断错误 **`schema.package_path`**（`"package path must belong to mount root <root>"`）。
3. **天然防遮蔽与私有边界隔离**：
   * 由于 `mounts` 以顶层根名为唯一主键（JSON 解析器在反序列化阶段直接拒绝重复键），且每个挂载单元内部的文档路径被严格限制在自身 `root` 之下，任何外部挂载均无法注入其他挂载根（如宿主预置根）下的包路径，从数据结构层面彻底杜绝了同名根包遮蔽或通过伪造同根路径越权访问其他根内 `_` 私有工作流与私有资源的可能。

#### 2.2.2 统一五阶段编译管线

在 [`compile_workflow`](../crates/axcutor-core/src/compiler/mod.rs) 中，所有挂载根无差别复用同一套五阶段编译管线：

1. **按需闭包发现与 Pass 1 归一化（`schema::root`）**：
   * 编译器首先解析各挂载根的 `resources` 构建全局资源符号表 $\Sigma_{\text{res}}$；
   * 随后从 `request.entry` 出发沿 `use: workflow:<pkg_path>` 递归发现跨根依赖的所有可达工作流文档；对每个被检查的文档 `id`，定位其所属挂载根 $\text{mount} = \text{request.mounts}[\text{root}(id)]$，并将该根的 `mount.allowed_executors.as_deref()` 与 `&resources` 传入 `schema::root`。
2. **Pass 2~5 静态检查与归一化回填（`check::root`）**：
   * 对所有已发现文档统一执行 `check::root`，完成符号解析、类型检查、SESE 拓扑检查、必达定值分析以及 `NodeBody::Run.input_schema` 与 `Route.kill_vars` 的归一化回填，生成完整的 `WorkflowBundle` 与 `CompileOutput.analysis`。

#### 2.2.3 按根 `allowed_executors` 白名单校验

1. **`MountUnit.allowed_executors` 契约**：
   * `None`（默认）：不限制该挂载根下工作流使用的执行器；
   * `Some(list)`：列表中的每个元素必须满足 `identifier(&s)`，仅作用于当前挂载根下 `packages` 中的文档；
   * 例如受控发行版可将外部编排挂载根配置为 `allowed_executors: Some(vec!["signal".into()])`，向其开放协作式挂起与恢复能力（[RFC-0017](./0017-cooperative-signal-executor.md)）的同时拦截 `"builtin"` 与 `"module"`；而预置库挂载根保持 `allowed_executors: None`（或 `Some(vec!["module".into()])`），在各自根内正交生效。
2. **Pass 1 静态拦截（`compiler/schema.rs`）**：
   * 解析某个挂载根下的工作流文档时，若遇到顶层 `run` 或图内节点 `run`，`schema::run()` 在校验完 `executor` 标识符后检查当前挂载根的 `allowed_executors`；
   * 若 `allowed_executors` 为 `Some(allow)` 且 `!allow.contains(&executor)`，在 `/run/executor`（或 `/nodes/<id>/run/executor`）处报告静态诊断错误 **`schema.executor_not_allowed`**（`format!("executor {executor} is not allowed in mount {root}")`）。

---

### 2.3 运行时与宿主层对齐（`axcutor_runtime` & `axcutor_host`）

由于执行器白名单与资源声明头文件已在编译期构造 `CompileRequest` 时完全收敛并锁定在生成的 `WorkflowBundle` 中，运行时（Worker）与宿主层只需对齐 `CompileRequest.mounts` 的数据结构与目录扫描，无需引入任何运行期白名单守卫参数：

1. **挂载目录自动发现 `__axcutor__.yaml` 与 `__ax_resources__.yaml`（[`packages.py`](../python/axcutor_runtime/src/axcutor_runtime/packages.py)）**：
   * `package_path` 校验将 `"__axcutor__"` 与 `"__ax_resources__"` 一并列为保留段名（禁止作为包路径段）；
   * `PackageMount` 可选携带 `allowed_executors: Sequence[str] | None = None`：
     ```python
     @dataclass(frozen=True)
     class PackageMount:
         name: str
         directory: Path
         allowed_executors: tuple[str, ...] | None = None
     ```
   * 新增 `snapshot_resources(mounts: Sequence[PackageMount]) -> dict[str, dict[str, str]]`，按与 `snapshot_packages` 相同的非符号链接与普通 Python 包祖先校验规则，从挂载目录树下自动扫描 `__ax_resources__.yaml`；
   * 新增 `snapshot_mounts(mounts: Sequence[PackageMount]) -> dict[str, JsonObject]`，按每个 `mount.name` 聚合该根下的 `packages`、`resources` 与 `allowed_executors`，直接生成符合 `CompileRequest.mounts` 结构的字典。
2. **编译、契约生成与宿主透传对齐（[`bundle.py`](../python/axcutor_runtime/src/axcutor_runtime/bundle.py) / [`contracts.py`](../python/axcutor_runtime/src/axcutor_runtime/contracts.py) / [`service.py`](../python/axcutor_host/src/axcutor_host/service.py)）**：
   * `compile_bundle(entry: str, mounts: Mapping[str, Any]) -> str` 直接向 `axcutor_core.compile_workflow` 提交 `{"entry": entry, "mounts": mounts}`；
   * `generate_contracts` 基于 `snapshot_mounts(mounts)` 编译完整闭包并生成 `_contract.py` 契约文件；
   * `ExecutionService.submit` 与 HTTP `/v1/executions`、CLI `axcutor run` 直接对齐按根组织的 `mounts` 结构；
   * [`axcutor-wasm`](../crates/axcutor-wasm/src/lib.rs) 的 `compile_workflow` 原生消费包含 `mounts: Ordered<MountUnit>` 的 `CompileRequest` JSON，无需修改任何 WASM 绑定代码即可在前端实时执行受控发行版的静态检查。
3. **`ResourceStore` 支持资源包内子模块定义并由 `__init__.py` 公开重导出（[`resources.py`](../python/axcutor_runtime/src/axcutor_runtime/resources.py)）**：
   * `ResourceStore` 仅接收原有的 `module_mounts`，**在运行期不读取磁盘上的 `__ax_resources__.yaml`**（与 `module.py` 不在运行期读取 `__axcutor__.yaml` 保持正交）；
   * `@resource` 允许类直接定义于资源包顶层 `<ModulePath>/__init__.py`，或定义于该资源包目录下的单文件子模块（包括 `_` 开头的私有实现模块，如 `<ModulePath>._tx`）中并由 `<ModulePath>/__init__.py` 公开导出（此时 `sys.modules[cls.__module__].__package__` 恒等于资源包的物理模块路径 `physical`）；
   * 正向解析 `resolve_resource_class("<ModulePath>:<ClassName>")`：通过 `load_module(physical, current_package=True)` 导入 `<ModulePath>/__init__.py` 获取 `<ClassName>`，校验 `cls.__qualname__ == <ClassName>` 且 `sys.modules[cls.__module__].__package__ == physical`（严格禁止跨包重导出）；
   * 反向推导 `_class_kind(instance)`：直接读取 `pkg = sys.modules[cls.__module__].__package__`，合成 `f"{logical_module(pkg, self.mounts)}:{cls.__name__}"`，并复核 `resolve_resource_class(kind) is cls`。

---

## 三、变更责任边界

| 模块路径 | 设计责任 |
| :--- | :--- |
| `crates/axcutor-core/src/ast/{mod,typespec}.rs` | `package_path` 与 `handle` `kind` 词法校验排除 `"__ax_resources__"` 保留段。 |
| `crates/axcutor-core/src/compiler/mod.rs` | `CompileRequest` 收敛为 `entry` 与 `mounts: Ordered<MountUnit>`（仅含 `packages`、`resources`、`allowed_executors`）；校验 `schema.mount_root` 与包路径归属不变式；所有可达挂载根统一复用 `schema::root` 与 `check::root`。 |
| `crates/axcutor-core/src/compiler/schema.rs` | 解析各挂载根的 `__ax_resources__.yaml` 构建全局 $\Sigma_{\text{res}}$ 并核对 `type.unknown_resource`；按挂载根的 `allowed_executors` 校验 `run` 执行器（`schema.executor_not_allowed`）。 |
| `python/axcutor_runtime/src/axcutor_runtime/packages.py` | 排除 `"__ax_resources__"` 保留段；`PackageMount` 支持可选 `allowed_executors`；新增 `snapshot_resources` 与 `snapshot_mounts` 自动扫描 `__ax_resources__.yaml` 并按根聚合挂载快照。 |
| `python/axcutor_runtime/src/axcutor_runtime/{bundle,contracts}.py` | `compile_bundle` 与 `generate_contracts` 对齐 `CompileRequest.mounts` 映射结构。 |
| `python/axcutor_runtime/src/axcutor_runtime/resources.py` | `ResourceStore` 基于 `module_mounts` 闭集与 `sys.modules[cls.__module__].__package__` 校验 `<ModulePath>/__init__.py` 导出的 `@resource` 类（支持同包单文件子模块定义并由 `__init__.py` 重导出，禁止跨包重导出，运行期不读 YAML）。 |
| `python/axcutor_host/src/axcutor_host/{service,cli,http}.py` | 对齐 `CompileRequest.mounts` 的提交通道与 CLI 挂载快照构造。 |
