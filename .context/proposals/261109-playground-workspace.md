# 基于文件 workspace 的 Playground

## 体验与职责边界

试用者执行 `git clone`、进入仓库并运行 `docker compose up` 后，打开 `http://localhost:8080`，即可浏览和编辑 workspace 文件、选择工作流入口与 JSON 输入文件、运行工作流，并查看现有 CFG、变量探针与执行帧。

Playground 由浏览器前端和 `playground/backend/` 内的独立 Python HTTP 后端组成，使用与 `axcutor run` 相同的文件发现、包挂载、编译、输入校验和 Execution 服务。网页提交文件路径，不再把浏览器中的包字典作为运行输入。普通 `/v1/executions` 接口和 CLI 继续保留各自现有职责。

依赖方向为 `playground backend → axcutor-host/runtime/core`。host 只提供通用执行能力，其包依赖、CLI、HTTP Schema 和 ExecutionService 不包含 workspace、Playground 或专属 Worker 管理逻辑。Playground 后端作为一个独立包提供 `axcutor-playground` 入口，相关 Python 测试也位于 `playground/` 内。

Playground 是受信任的本地开发工具。多个页面共享同一 workspace；不增加用户账户、浏览器会话工作区或远程 IDE 协议。

## 文件与存储模型

Playground 容器将仓库 `examples/` 只读挂载到 `/examples`，将命名卷挂载到 `/data`。首次启动将示例复制到 `/data/workspace`；已有 workspace 时保留用户修改。宿主仓库不会因网页编辑而变化。

```text
/data/
└── workspace/                     # 网页编辑的持久化源文件
    ├── workflows/
    │   └── demo/
    │       ├── __init__.py
    │       ├── main/
    │       │   ├── __axcutor__.yaml
    │       │   └── summarize.py    # module 业务实现
    │       └── double/
    │           ├── __axcutor__.yaml
    │           └── __init__.py     # module 业务实现
    └── inputs/demo.json
```

`workspace/workflows/` 下的公开 Python 根包自动形成显式 `PackageMount`，例如 `demo → workflows/demo`。包发现和入口转换复用 `load_packages`，保留 `__axcutor__.yaml`、`__init__.py`、私有可见性与命名冲突规则。入口也可为独立 YAML/JSON 文件，沿用 `__main__` 限制。

文件接口只接受 workspace 内的相对路径及 UTF-8 文本源文件，不接受绝对路径、上级跳转、符号链接或特殊文件。生成契约在文件树中可查看但不可手工修改；运行时从当前工作流重新生成契约。新增文件可同时创建父目录，删除只针对具体文件。

文件内容和路径的确定性摘要作为 workspace revision。保存携带读取时的 revision；过期写入返回 `409`，页面保留未保存内容并提示重新加载，避免多个浏览器静默覆盖。单个后端锁串行化文件保存与 Run 前准备；运行中拒绝保存，避免正在执行的模块读取到中途修改的文件。

## 单次运行与模块重导入

Playground 后端启动一个常驻 Temporal Worker 和本次后端生命周期使用的队列，Run 复用该 Worker。同一 Playground 一次只接受一个根 workflow；单次 workflow 内部的图并发保持原样。忙时保存和新的 Run 返回 `409`，不挂起 HTTP 请求或建立等待队列。读取、观察和 Cancel 继续可用。

Run 从已保存 workspace 发现包，在其中生成契约，再完成编译和输入预检。生成文件通过生成器更新，并同步最新文件视图与 revision；提交和运行期间文件编辑器只读，不维护在途编辑缓冲。工作流源码继续由 ExecutionService 编译为提交快照，Python 直接从 workspace 导入，不另存运行目录或完整代码副本。

每次 Run 前清除 workspace 自有业务包、辅助模块和生成契约的导入缓存，包括已删除文件的旧模块；删除 workspace 内的字节码缓存并使导入查找缓存失效，避免快速同长度修改仍命中旧代码。保留 host/runtime/第三方库缓存，随后使用普通 Python 导入。此准备只发生在两次运行之间，不需要文件监听、增量 reload、版本命名空间或自定义 importer。

Worker 使用 runtime 公开的 `create_worker(executors=...)` 和 `module_executor(...)`。Playground 通过薄转发函数在 Run 边界更新模块执行器委托，不修改 host/runtime。后端只保留当前 execution ID、完成等待任务和本地在途调用状态；根执行终态且本地执行器清理完成后解除忙状态，不能因取消请求已确认而提前解除。

后端正常关闭时尽力取消当前演示执行、等待清理并关闭 Worker。重启使用新队列，不恢复旧演示，不保存恢复记录或运行结果缓存；执行记录、历史和结果仍由 Temporal 保存。关闭网页只停止观察，Cancel 继续复用现有执行取消能力。

## Workspace HTTP 接口

独立 `axcutor-playground` 后端直接声明 workspace、Execution 查询与取消、静态页面路由及其错误适配。执行提交、查询和取消调用 ExecutionService 的公开方法；预检复用 runtime/core 的公开编译和输入校验能力。后端不导入 host 私有 HTTP 工具，也不通过创建并删改 host 应用路由来组装自身接口。host 的 `axcutor serve` 只提供普通 Execution API。

| 接口 | 契约 |
| --- | --- |
| `GET /v1/workspace` | 返回相对文件路径、文本、生成文件标记和 revision；运行中仍可读取。 |
| `PUT /v1/workspace` | 接收 revision 与编辑后的文件集合；预检路径和只读文件，保存后返回新 workspace。缺少的普通文件表示删除；运行中返回 `409`。 |
| `POST /v1/workspace/executions` | 接收入口相对路径、可选输入文件路径及 workspace revision；准备当前文件并提交，已有运行时返回 `409`。 |
| `GET /v1/executions/{id}` | 沿用现有 Execution 状态投影。 |
| `POST /v1/executions/{id}:cancel` | 沿用现有协同取消。 |

运行响应包含 Execution 与实际提交的 `entry + packages` 工作流源码视图。前端据此建立 Submitted snapshot，保证画布对应服务端实际执行的工作流；该视图不保存 Python 代码版本。错误继续使用现有错误信封与编译诊断结构。Input 从 workspace 读取并通过 Python lossless codec 解码，响应变量与输出继续保留原始 JSON token。

独立后端维护包含以上接口的 OpenAPI 描述，通过 host 的公开 Schema 函数复用基础 Execution Schema。通过现有生成命令更新 OpenAPI JSON 和 Hey API SDK，不手改生成文件。WASM 的编译接口和 Rust 数据结构无需改变。

## 网页文件工作台

左栏改为 workspace 文件树，以目录层级展示 YAML、JSON、Python 和生成契约，提供文件选择、新建、删除、保存和重新加载。编辑器显示相对路径、未保存状态及只读状态；提交、运行和清理期间文件内容只读，新建、删除和保存不可用。文件浏览、观察和取消继续可用。不再提供与物理文件脱节的包名和格式编辑器。

入口选择器使用工作流文件路径，输入选择器使用 JSON 文件路径，可选择空输入 `{}`。编辑 JSON 输入与编辑其他文件使用同一个文件编辑器。首次打开默认选择 `workflows/demo/main/__axcutor__.yaml` 与 `inputs/demo.json`。

浏览器从当前文件缓冲构造 WASM `entry + packages` 预览，保留实时诊断、入口契约、CFG 导航和变量探针。包名与文件路径之间有明确映射，点击编译诊断时定位真实源码文件。Python 编辑不触发模块导入，实际运行仍由宿主完成。

Run 先保存当前修改，再携带新 revision 请求运行。发生保存冲突时不提交执行。服务端返回的源码视图用于生成 Submitted snapshot，随后复用现有执行状态轮询、取消与帧联动。提交、运行和清理期间禁止文件编辑、新建、删除、保存和再次 Run，结束后恢复操作。后续编辑切回 Editor compilation，两个画布来源继续明确区分。

## Docker 构建与默认示例

Docker 多阶段构建增加 WASM 与前端构建步骤，在镜像内安装固定版本工具、生成绑定并构建静态页面。运行镜像不依赖宿主的 Node.js、Rust、uv 或预先生成的本地产物。`.dockerignore` 排除本地前端依赖和生成目录，保证干净 clone 可构建。

Compose 新增默认启动的 Playground 服务，暴露 `8080`，与现有 Temporal 服务通信，拥有 `/examples:ro` 和独立数据卷。统一镜像安装独立的 `axcutor-host` 与 Playground 后端包，并包含静态页面与示例包。普通服务使用 `axcutor worker`、`axcutor serve` 和 `axcutor run`；Playground 服务使用自己的 `axcutor-playground` 入口。软件包与命令职责独立，不增加服务拓扑。网页与 API 同源，生产运行不依赖 Vite 开发服务器。

默认 `demo.main` 保留并发处理 `[3,1,2]`、汇总得到 `[6,2,4]` 和 `12` 的控制流，将 Python 倍增与汇总 Activity 改为文件中的 `module` 实现，并通过现有契约生成器更新契约文件。基础 Worker 镜像包含可导入的示例包，使原有 Docker CLI 示例也可继续执行；Playground 则在 Run 边界重新导入 workspace 中的业务模块。

README 与已有 Docker、Playground 文档同步描述 clone、Compose、网页入口和独立后端的本地启动方式，并明确 workspace 与 Temporal 历史分别由哪些卷保存。OpenAPI 生成、浏览器辅助脚本、Python 包配置和测试入口均指向独立后端。
