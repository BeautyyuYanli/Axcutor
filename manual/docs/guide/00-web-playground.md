# Web Playground

Playground 是基于真实文件的本地工作台。多个页面共享一个持久化 workspace，可编辑 YAML、JSON 和 Python，使用 WASM 实时预览 CFG 与诊断。Run 使用与 CLI 相同的文件加载、编译、输入校验和 module 执行器；执行状态、历史与结果由 Temporal 保存。Playground 另在进程内存中保留当前或最近一轮的 trace、结果与提交时工作流快照，页面刷新可恢复，后端重启清空。

浏览器前端位于 `playground/`，独立 Python 包位于 `playground/backend/`，入口为 `axcutor-playground`。该包单向依赖 `axcutor-host/runtime/core`，负责 workspace、文件 API 和 Worker 生命周期；普通 host/runtime 不依赖 Playground。相关 Python 测试位于 `playground/backend/tests/`。

## 使用 Docker 启动

在克隆后的仓库根目录执行：

```sh
# 首次启动在镜像内构建原生扩展、WASM 和静态网页。
docker compose up
```

打开 `http://localhost:8080`。页面与 API 同源，不需要 Vite 或宿主机的 Node.js、Rust、uv。端口由 `AXCUTOR_PLAYGROUND_PORT` 调整。默认入口为 `workflows/demo/main/__axcutor__.yaml`，输入为 `inputs/demo.json`；并发调用倍增 module 后，`summarize.py` 汇总得到 `[6,2,4]` 与 `12`。

## 工作台布局

页面顶部直接显示运行配置行：Entry、Input file、Run 和 Cancel；编译加载或异常时才显示状态提示。Entry 展示 `demo.main` 等模块名，独立文档显示 `__main__`；悬停可查看对应文件路径，提交仍使用实际文件路径。

桌面主区域由文件侧栏、源码编辑区与持续显示的流程画布组成。文件树独立滚动，当前文件有选中标记，悬停文件或目录可查看完整路径；Delete file 位于文件区内，新建文件表单位于侧栏底部。Editor 内提供 Save files、Discard edits、保存状态、当前路径及必要的生成文件或只读提示，原生文本框占据剩余高度。

Workspace files 和 Editor 默认展开，通过各自标题按钮收起或展开。收起后仅保留本区域的图标入口，悬停显示名称，仍支持键盘展开。折叠保留未保存内容、编辑位置、画布来源与执行观察；点击文件、新建成功或定位诊断会展开编辑区。例如：

```text
Editor → 收起源码区 → 点击文件 → 展开该文件的源码
Workspace files → Editor → 画布获得更多空间 → Editor → 恢复原编辑内容
```

流程图从左到右排列，节点使用紧凑卡片，Start、End 和 Fail 使用小标记。顶部保留工作流选择、Zoom out、Zoom in 与 Fit graph；作用域面包屑仅在进入内联子图后出现。区域尺寸变化后自动适配当前图。节点标题旁的箭头进入工作流或子图，也可双击节点进入；所有入口支持键盘操作。配置了并行遍历、重试或超时时，分别显示 Parallel items、Retries enabled、Timeout configured 行为标签，不列出参数数值；运行中的并行项在同一位置显示索引。蓝色外框表示选中，绿色标记表示运行，两种状态可以同时出现。

底部 Trace 面板使用共享时间刻度，按父子层级展示每次真实调用的状态、持续时间与并行条，调用详情位于时间线旁；窄屏改为上下排列。查看 Trace 时上方流程画布继续显示。Zoom trace in / out 放大或缩小时间轴，Fit trace 恢复完整范围，窄屏在时间线内部滚动。零时间调用显示为可选择的点，耗时仍为 `0 ms`。开始、结束与事件时间来自可重放的 Workflow 时钟；未结束条延伸至 Playground 本次查询的 UTC 观测时间，耗时标记为 observed，已结束条固定使用记录的起止时间。这些时间不表示 CPU 时间或独立执行器性能指标。例如：

```text
Trace → 选择 copy · builtin → 查看独立 Input / Output、状态和耗时
Trace → Timing → 展开开始/结束时间
Trace → 选择已结束的并行项 → Locate in Flow → 定位提交快照中的 CFG 节点
```

调用详情位于 Trace 面板，结束后仍可选择；轮询保持选中调用。默认突出状态、耗时与 Input / Output，精确起止时间在 Timing 中默认折叠。Locate in Flow 定位上方画布，保留 Trace 与详情。失败、取消和尚未返回输出分别说明，无输出字段的成功调用显示空输出。等待、解释器重试及 catch 事件在详情中展示；Temporal 原生 Activity 重试仅显示已知策略，不推测每次尝试的次数或时间。

底部面板默认收起，点击 Execution、Trace 或 Diagnostics（问题数量）即可展开并切换内容；再次点击当前标签收起，再次点击恢复。例如：

```text
Run → 展开 Execution，观察当前步骤、所选步骤数据与结果
新的编译问题或明确提交失败 → 展开 Diagnostics，查看错误或定位源码
Diagnostics → Diagnostics → 收起 → Diagnostics → 恢复诊断内容
```

Execution 标题持续显示 Not run、Submitting、Observing、Running、Waiting for signal、Cancelling、Succeeded、Failed 或 Cancelled；面板收起时状态仍可见。仅在观察失败、等待取消、其他页面占用或终态后仍在清理等情况下额外显示说明。提交期间隐藏上一次结果，只收到 ID 或查询暂时失败时继续观察并允许取消。执行终态后 workspace 仍忙时，提示本地工作或清理尚未结束，文件继续只读。

切换或收起面板保持执行观察与编译状态，后台轮询不会重新选择面板。提交结果不确定且已取得 execution ID 时，页面保持 Execution 可见并继续观察，顶部 Cancel 可取消该执行。

桌面工作台使用视口高度，各区域分别滚动；长路径在路径栏换行，表格、源码长行与原始 JSON 在所在区域内滚动。中等宽度下源码与 CFG 上下排列；手机宽度下文件、源码、CFG 与底部面板依次排列，使用页面滚动。

## 文件、保存与共享

`examples/` 只读挂载到 `/examples`，`playground/workspace/` 挂载到 `/workspace`。目录在仓库中仅保留 `.gitkeep`；首次启动将示例复制到 `playground/workspace/`，后续可在网页或宿主机直接编辑该目录下的文件，修改不被纳入 Git 跟踪。

文件树展示实际目录与文件，YAML、JSON 和 Python 共用文本编辑器。Create file 可同时创建父目录，Delete file 只删除所选普通文件。接口接受 UTF-8 的 `.py`、`.yaml`、`.yml` 和 `.json` 相对路径，拒绝绝对路径、上级跳转、符号链接与特殊文件。Python 编译缓存不属于源文件，模板复制跳过 `__pycache__/`；该目录名不能用于新建源文件。`_contract.py` 和 `*_contract.py` 为只读生成文件。

`workflows/` 下含 `__init__.py` 的公开根包自动形成显式挂载：

```text
workflows/demo/main/__axcutor__.yaml   → demo.main
workflows/demo/double/__axcutor__.yaml → demo.double
scratch/main.json                    → __main__
```

Editor 中的 Save files 保存当前全部文件，Discard edits 重新读取已保存文件；Run 先保存，再请求执行。保存与 Run 都携带 workspace revision。多个页面修改时，过期请求返回 `409 / workspace_conflict`，页面保留本地内容，不提交本次执行。可先复制需要保留的文本，再点击 Discard edits 重新读取共享文件。刷新页面也会读取已保存文件。

提交准备、运行和本地清理期间，文件编辑器只读，新建、删除、保存和再次 Run 均不可用；文件浏览、GET 与 Cancel 保持可用。后端在忙时直接拒绝保存和新 Run，返回 `409`。Run 前先保存已有编辑，生成契约后直接更新文件视图和 revision。

## 环境与生成

需要 Node.js 22.18 或更高版本、Python 3.11 或更高版本、uv、Rust、`wasm32-unknown-unknown` 标准库与匹配的 wasm-bindgen CLI。WASM 已使用 Rust 1.90.0 验证。以下命令均从仓库根目录执行：

```sh
# 安装浏览器目标与 Cargo.toml 中固定版本的绑定工具。
rustup target add wasm32-unknown-unknown
cargo install wasm-bindgen-cli --version 0.2.129 --locked

# 安装锁定的 Python 与前端依赖。
uv sync --locked
npm --prefix playground ci

# 从宿主导出 OpenAPI，再生成 SDK 与浏览器 WASM 绑定。
npm --prefix playground run generate
npm --prefix playground run build
```

`generate:client` 调用 `python -m axcutor_playground.http --output openapi.json`，随后用固定版本的 Hey API 生成 `playground/src/client/`。当前 Hey API 将 Fetch client 随生成器内置，因此不另安装已弃用的 `@hey-api/client-fetch` 包。

`generate:wasm` 使用 `cargo build --locked --release -p axcutor-wasm --target wasm32-unknown-unknown` 与 `wasm-bindgen --target web` 生成 `playground/src/wasm-pkg/`。脚本从仓库 `Cargo.toml` 读取绑定版本并核对 CLI；可通过 `AXCUTOR_CARGO`、`AXCUTOR_WASM_BINDGEN` 指定可执行文件，支持 `CARGO_TARGET_DIR`。生成的 WASM 与胶水代码不纳入 Git，首次运行前必须生成；修改 Rust 后应重新运行该命令。OpenAPI 和 SDK 纳入 Git，修改宿主协议后应重新生成，不手工编辑产物。

## 本机开发

```sh
# 启动 Temporal。独立终端运行 Playground 后端，默认端口为 8080。
docker compose up -d temporal
uv run --locked axcutor-playground

# 启动开发页面；访问终端输出的 http://127.0.0.1:5173/。
npm --prefix playground run dev
```

后端默认使用 `playground/workspace/` 保存 workspace、`examples/` 提供模板、`playground/dist/` 提供静态网页，可通过 `--workspace`、`--examples`、`--static` 指定路径。Vite 默认将 `/v1` 和 `/openapi.json` 代理至 `http://127.0.0.1:8080`；更换 API 地址时使用：

```sh
AXCUTOR_API_URL=http://127.0.0.1:9000 npm --prefix playground run dev
```

生产静态文件由 `axcutor-playground` 直接提供；`vite preview` 仅用于预览静态构建。

只安装普通宿主时可使用 `uv sync --locked --no-dev --package axcutor-host`；安装独立后端及其依赖时使用 `uv sync --locked --no-dev --package axcutor-playground`。根目录的默认开发安装包含二者。

## 编辑、检查与执行

1. 在顶部配置行用 Entry 按模块名选择工作流；Input file 选择 JSON 文件路径，或 Empty input `{}`。输入文件与其他源码使用同一个编辑器。独立 YAML/JSON 入口使用 `__main__`；未被入口闭包引用的文档不参与编译。
2. 当前缓冲自动构造 `entry + packages` 交给 WASM，Python 编辑不导入模块。底部 Diagnostics 中的包名映射到实际源码文件，点击诊断展开编辑区并定位文件与节点。
3. 点击 CFG 节点选中节点；双击 workflow/graph 节点或点击 Open 按钮进入目标工作流/内联图，面包屑返回上层作用域。编译器的类型和静态分析用于图与诊断，不在界面另列内部分析表格。
4. Run 先保存文件，再携带新 revision 提交路径。响应包含实际提交的源码视图，页面据此构造 Submitted snapshot。运行中每隔 300ms 查询状态；Execution 按父子关系列出当前步骤，以工作流、节点名称及并行项索引区分，默认选择一个真实的当前步骤。点击当前步骤展示其当前字段并跳转对应 CFG；当前帧消失后改选仍活跃的步骤。Trace 则保留每次已经发生的调用，独立展示真实 Input / Output，循环和重试不会按地址合并，短于轮询间隔的步骤也能在结束后查看。
5. 成功时突出最终输出及实际返回分支，失败时突出原因，取消时显示已取消；执行 ID 与时间位于默认收起的 Execution details。Cancel 发起协同取消。服务端返回 `202 / running` 表示仍在清理，页面继续轮询并禁用重复取消；进入 `completed`、`failed` 或 `cancelled` 时停止查询普通执行状态。Trace 独立观察到最终记录后停止，失败或断连时保留已有数据并重试，也可点击 Retry trace。页面继续观察 workspace 忙状态，直至本地执行器也清理完成才恢复编辑、新建、删除、保存和 Run。

画布来源自动选择：提交或恢复运行时使用该轮工作流快照，选择或编辑源码时使用当前编辑内容，点击执行步骤或 Locate in Flow 时使用对应运行快照。Trace 自身始终对应该轮提交，查看 Trace 不覆盖正在编辑的画布。离开页面只停止观察，不会取消已提交的执行；刷新或重开页面会加载本次后端启动期间最近一轮及快照。

`signal` 节点挂起时，标题与步骤显示 `Waiting for signal`，所选步骤的数据区显示精确等待地址，workspace 继续保持忙状态。通过 [Execution 信号接口](../reference/04-execution-http-api.md#投递外部信号) 选择地址并提交输出，例如：

```http
POST /v1/executions/order-001:signal
Content-Type: application/json

{"address":"__main__/decision","output":{"reviewer":"alice"},"step":2}
```

接口沿用宿主的 `400`、`409`、`422`、`503` 错误契约，浏览器的后续轮询会展示恢复后的状态。工作流终态及本地清理结束后恢复文件编辑。

匿名入口 `__main__` 中的 `module` 仍在编译期被拒绝，包挂载、私有可见性与模块布局沿用 [包与模块](./01-foundations/04-packages-and-modules.md)。

## 常驻 Worker 与再次运行

后端启动一个常驻 Worker，创建本次后端生命周期使用的队列，所有 Run 复用该 Worker。同一 Playground 一次只接受一个根 workflow，单次图内部并发不变。Run 直接在 workspace 中发现包、生成契约、编译并校验输入，不复制运行目录。例如：

```python
# 当前演示清理完成后编辑并保存，下一次 Run 导入新 Python。
return Output(value=input.value * 3)
```

每次 Run 前清除 workspace 自有业务包、辅助模块和契约的导入缓存，包括已删除文件的旧模块；清除 workspace 字节码缓存并使导入查找缓存失效，再按普通 Python 规则导入。host、runtime 和第三方库缓存保持原样。若 workspace 根包名已被非 workspace 模块占用，Run 拒绝该名称。运行中不 reload 模块；运行期间外部文件编辑器应避免并发修改该目录，在宿主机修改文件后可在 Editor 中点击 Discard edits 或刷新页面同步最新内容。

Submitted snapshot 保留实际提交的工作流 `entry + packages`，Python 源码不进入执行历史。工作流与 Python 文件在两次运行之间编辑，修改在后续 Run 生效。通用 module 执行器仍使用标准导入缓存，此 Run 边界由独立 Playground 后端负责。

正常关闭后端会尽力取消当前演示，等待本地清理并关闭 Worker。最近一轮缓存仅在进程内存中存在，不写入数据库、文件或浏览器历史；没有运行列表。新执行在预检通过并分配 ID 后替换上一轮，保存、编译或输入预检失败保留原记录。提交结果不确定但已取得 ID 时，缓存及后续观察均指向该 ID；页面可从最近一轮接口取得对应源码快照，不会自动重新提交。无法确认该 ID 终态时，workspace 保持忙状态。

重启后端会清空最近一轮缓存并创建新队列，不恢复旧演示。普通 ExecutionService 仍为无缓存通用服务，已关闭执行的 get / wait 仅依赖 Temporal history，无需 Worker。Playground 显式启用 runtime 的可选 `record_trace`；累计 trace 查询（包括已关闭 Workflow 与其 Child Workflow）依赖常驻 Worker，最终记录取得后由 Playground 内存保留，读取最终缓存无需再次查询 Worker。追踪失败不改变执行终态或 workspace 清理锁。

`playground/workspace/` 保存持久化 workspace 文件；`temporal-data` 保存执行历史与结果。`docker compose down` 保留二者，`docker compose down -v` 删除 `temporal-data` 卷。Playground 是受信任的本地演示工具，不提供账户、每浏览器独立 workspace 或旧演示恢复保证。

## Workspace HTTP 接口

独立 `axcutor-playground` 后端直接声明以下接口与静态页面路由，调用 ExecutionService 公开方法，并负责自己的错误适配；普通 `axcutor serve` 不启用文件写入。OpenAPI 复用 host 的公开 Execution Schema，错误沿用统一信封与编译诊断结构。

| 方法与路径 | 行为 |
| --- | --- |
| `GET /v1/workspace` | 返回文件路径、文本、生成标记、revision、busy 和当前 execution_id。 |
| `PUT /v1/workspace` | 接收 revision 与完整 `{path,text}` 文件集合，缺少的普通文件表示删除；忙时返回 409。 |
| `POST /v1/workspace/executions` | 接收 entry、可选 input 文件路径和 revision，返回 Execution、实际 source 与生成后的 workspace；忙时返回 409。 |
| `GET /v1/workspace/trace` | 返回当前或最近一轮的 execution_id、Execution（提交结果未知时可为 null）、source、累计 spans、observed_at、complete 与 trace_error；无记录时返回 null。可传 execution_id 查询参数校验该轮，已替换或重启清空时返回 404。 |
| `GET /v1/executions/{id}` | 沿用现有状态投影。 |
| `POST /v1/executions/{id}:cancel` | 沿用现有协同取消。 |
| `POST /v1/executions/{id}:signal` | 向 `Frame.waiting` 地址投递输出和可选分支、步数，沿用宿主信号契约。 |

```json
{"entry":"workflows/demo/main/__axcutor__.yaml","input":"inputs/demo.json","revision":"<GET 或保存返回的 revision>"}
```

## 无损 JSON

输入以原始文本文件保存，浏览器只提交路径，由 Python lossless codec 解码。以下数值及对象键序不会经 JavaScript 浮点数或对象枚举转换：

```json
{"integer":99999999999999999999999999999999999,"number":1.0,"ordered":{"2":2,"1":1,"z":3}}
```

重复对象键、非有限数值、非法 JSON 和非对象 Input 会在提交前报错。响应中的 `frames[].variables`、`result.output`、trace 的 `spans[].input` / `output` 和错误详情按原始 token 提取并仅调整空白；字段表也保留字段与嵌套对象的原始键序及数值精度。宿主可规范化等价的十进制写法，数值与 integer/number 类别遵循 [HTTP API 无损 JSON 契约](../reference/04-execution-http-api.md)。WASM 编译与查询 API 继续使用原始 JSON 契约，直接查询测试保留枚举数值与键序的校验。

## 验证

```sh
# TypeScript、生产构建、真实 WASM 与客户端边界测试。
npm --prefix playground run build
npm --prefix playground test

# 使用真实 HTTP、文件目录、常驻 Worker 与临时 Temporal Server 执行 Chromium 测试。
# 可设置 AXCUTOR_CHROME 指向已安装的 Chrome/Chromium。
npm --prefix playground exec -- playwright install chromium
npm --prefix playground run test:browser

# 根测试入口同时覆盖普通宿主与独立后端。
uv run --locked pytest
uv run --locked ruff check python tests/runtime tests/host tests/conftest.py tests/helpers.py playground/backend playground/scripts
uv run --locked pyrefly check
```

浏览器测试会选择临时 HTTP、页面端口和 workspace 目录，结束时关闭测试 Worker 和 Server。覆盖保存与重载、跨页面冲突、提交和运行期间禁止编辑与文件操作、完成或取消清理后恢复编辑、Python 与契约修改后重跑、工作流提交视图、CFG/步骤联动及无损 JSON；同时检查文件区与编辑区独立折叠、恢复后内容与位置保留、文件与诊断重开编辑区、生命周期提示、面板切换与收起、轮询期间保留面板选择、键盘焦点、缩放及长内容溢出约束。布局与双折叠画布截图覆盖 1600×1000、1280×800、900×900 与 390×844，并包含步骤数据、成功结果与失败界面。Temporal 测试服务器与 Playwright 浏览器首次使用时需要下载；截图位于 `playground/.browser-test/`。

后端集成测试默认下载并启动 Temporal CLI v1.6.1 的开发服务器，验证 Worker 复用、再次导入、图内并发、取消清理、最近一轮替换与预检保留、关闭后的 Child Workflow trace 及追踪故障重试。runtime 测试使用真实 Temporal 和 history 重放，覆盖快速循环、无损值、并行重试及失败/取消的部分 trace。浏览器检查时间线、零时间调用、独立 Input / Output、缩放/定位与页面重开恢复，并将 trace 截图存入同一目录。设置 `AXCUTOR_TEST_TEMPORAL_ADDRESS` 可改用已有的测试专用服务器；测试使用独立执行 ID 和队列。

仅运行独立后端测试时使用 `uv run --locked pytest playground/backend/tests`。根 pytest、Pyrefly 与 CI 已包含该包；CI 的静态检查也覆盖其源码、测试和浏览器辅助脚本。

也可设置 `AXCUTOR_BROWSER_URL=http://127.0.0.1:8080`，对已启动的 Compose 生产网页运行同一套检查。此模式会编辑目标 workspace，应使用隔离的测试项目与工作区目录。
