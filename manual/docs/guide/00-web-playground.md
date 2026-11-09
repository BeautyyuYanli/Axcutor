# Web Playground

Playground 是基于真实文件的本地工作台。多个页面共享一个持久化 workspace，可编辑 YAML、JSON 和 Python，使用 WASM 实时预览 CFG、诊断与静态变量。Run 使用与 CLI 相同的文件加载、编译、输入校验和 module 执行器；执行状态、历史与结果由 Temporal 保存。

浏览器前端位于 `playground/`，独立 Python 包位于 `playground/backend/`，入口为 `axcutor-playground`。该包单向依赖 `axcutor-host/runtime/core`，负责 workspace、文件 API 和 Worker 生命周期；普通 host/runtime 不依赖 Playground。相关 Python 测试位于 `playground/backend/tests/`。

## 使用 Docker 启动

在克隆后的仓库根目录执行：

```sh
# 首次启动在镜像内构建原生扩展、WASM 和静态网页。
docker compose up
```

打开 `http://localhost:8080`。页面与 API 同源，不需要 Vite 或宿主机的 Node.js、Rust、uv。端口由 `AXCUTOR_PLAYGROUND_PORT` 调整。默认入口为 `workflows/demo/main/__axcutor__.yaml`，输入为 `inputs/demo.json`；并发调用倍增 module 后，`summarize.py` 汇总得到 `[6,2,4]` 与 `12`。

## 文件、保存与共享

`examples/` 只读挂载到 `/examples`。首次启动复制到 `playground-data` 卷的 `/data/workspace`，后续保留网页修改，不写回仓库。

文件树展示实际目录与文件，YAML、JSON 和 Python 共用文本编辑器。Create file 可同时创建父目录，Delete file 只删除所选普通文件。接口接受 UTF-8 的 `.py`、`.yaml`、`.yml` 和 `.json` 相对路径，拒绝绝对路径、上级跳转、符号链接与特殊文件。Python 编译缓存不属于源文件，模板复制跳过 `__pycache__/`；该目录名不能用于新建源文件。`_contract.py` 和 `*_contract.py` 为只读生成文件。

`workflows/` 下含 `__init__.py` 的公开根包自动形成显式挂载：

```text
workflows/demo/main/__axcutor__.yaml   → demo.main
workflows/demo/double/__axcutor__.yaml → demo.double
scratch/main.json                    → __main__
```

Save files 保存当前全部文件；Run 先保存，再请求执行。保存与 Run 都携带 workspace revision。多个页面修改时，过期请求返回 `409 / workspace_conflict`，页面保留本地内容，不提交本次执行。可先复制需要保留的文本，再点击 Reload (discard edits) 重新读取共享文件。刷新页面也会读取已保存文件。

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

后端默认使用 `.axcutor/` 保存数据、`examples/` 提供模板、`playground/dist/` 提供静态网页，可通过 `--data`、`--examples`、`--static` 指定路径。Vite 默认将 `/v1` 和 `/openapi.json` 代理至 `http://127.0.0.1:8080`；更换 API 地址时使用：

```sh
AXCUTOR_API_URL=http://127.0.0.1:9000 npm --prefix playground run dev
```

生产静态文件由 `axcutor-playground` 直接提供；`vite preview` 仅用于预览静态构建。

只安装普通宿主时可使用 `uv sync --locked --no-dev --package axcutor-host`；安装独立后端及其依赖时使用 `uv sync --locked --no-dev --package axcutor-playground`。根目录的默认开发安装包含二者。

## 编辑、检查与执行

1. Entry 选择工作流文件路径；Input file 选择 JSON 文件路径，或 Empty input `{}`。输入文件与其他源码使用同一个编辑器。独立 YAML/JSON 入口使用 `__main__`；未被入口闭包引用的文档不参与编译。
2. 当前缓冲自动构造 `entry + packages` 交给 WASM，Python 编辑不导入模块。诊断中的包名映射到实际源码文件，点击诊断定位文件与节点。文件区可展开入口的字段契约。
3. 点击 CFG 节点查看 `definite_in`、注入项、派生端口及可用变量投影；Expected type 选择器演示 WASM 的子类型过滤。双击 workflow/graph 节点或点击 Open 按钮进入目标工作流/内联图，面包屑返回上层作用域。
4. Run 先保存文件，再携带新 revision 提交路径。响应包含实际提交的源码视图，页面据此构造 Submitted snapshot。运行中每隔 300ms 查询一次；右侧展示活跃帧的地址、游标、调用深度、步数与变量。点击帧可跳转到对应 CFG；在执行快照中点击节点也会选中对应活跃帧。并发节点显示在途迭代索引。
5. Cancel 发起协同取消。服务端返回 `202 / running` 表示仍在清理，页面继续轮询并禁用重复取消；进入 `completed`、`failed` 或 `cancelled` 时停止查询执行。页面继续观察 workspace 忙状态，直至本地执行器也清理完成才恢复编辑、新建、删除、保存和 Run。

Canvas source 的 Submitted snapshot 始终对应提交时的编译结果。清理结束后编辑源码会切换到 Editor compilation；点击帧可返回执行快照。离开页面只停止观察，不会取消已提交的执行。

`signal` 节点挂起时，帧树显示 `Waiting for signal` 及精确等待地址，workspace 继续保持忙状态。通过 [Execution 信号接口](../reference/04-execution-http-api.md#投递外部信号) 选择地址并提交输出，例如：

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

每次 Run 前清除 workspace 自有业务包、辅助模块和契约的导入缓存，包括已删除文件的旧模块；清除 workspace 字节码缓存并使导入查找缓存失效，再按普通 Python 规则导入。host、runtime 和第三方库缓存保持原样。若 workspace 根包名已被非 workspace 模块占用，Run 拒绝该名称。运行中不 reload 模块；持久化文件只由后端写入，外部文件编辑器应停止写入该目录。

Submitted snapshot 保留实际提交的工作流 `entry + packages`，Python 源码不进入执行历史。工作流与 Python 文件在两次运行之间编辑，修改在后续 Run 生效。通用 module 执行器仍使用标准导入缓存，此 Run 边界由独立 Playground 后端负责。

正常关闭后端会尽力取消当前演示，等待本地清理并关闭 Worker。重启创建新队列，不恢复旧演示，不保存恢复凭据或执行结果。提交结果不确定时，错误详情保留 `execution_id`，可查询或取消原 ID；查询失败不表示执行未创建，也不会自动重新提交。无法确认该 ID 终态时，workspace 保持忙状态，重启后端可开始新的演示。

`playground-data` 只保存持久化 workspace；`temporal-data` 保存执行历史与结果。`docker compose down` 保留二者，`docker compose down -v` 删除二者。Playground 是受信任的本地演示工具，不提供账户、每浏览器独立 workspace 或旧演示恢复保证。

## Workspace HTTP 接口

独立 `axcutor-playground` 后端直接声明以下接口与静态页面路由，调用 ExecutionService 公开方法，并负责自己的错误适配；普通 `axcutor serve` 不启用文件写入。OpenAPI 复用 host 的公开 Execution Schema，错误沿用统一信封与编译诊断结构。

| 方法与路径 | 行为 |
| --- | --- |
| `GET /v1/workspace` | 返回文件路径、文本、生成标记、revision、busy 和当前 execution_id。 |
| `PUT /v1/workspace` | 接收 revision 与完整 `{path,text}` 文件集合，缺少的普通文件表示删除；忙时返回 409。 |
| `POST /v1/workspace/executions` | 接收 entry、可选 input 文件路径和 revision，返回 Execution、实际 source 与生成后的 workspace；忙时返回 409。 |
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

重复对象键、非有限数值、非法 JSON 和非对象 Input 会在提交前报错。响应中的 `frames[].variables`、`result.output` 和错误详情按原始 token 提取并仅调整空白。宿主可规范化等价的十进制写法，数值与 integer/number 类别遵循 [HTTP API 无损 JSON 契约](../reference/04-execution-http-api.md)。编译结果中的原始 analysis 也直接交回 WASM，保留 TypeSpec 枚举的数值与键序。

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
uv run --locked mypy
```

浏览器测试会选择临时 HTTP、页面端口和数据目录，结束时关闭测试 Worker 和 Server。覆盖保存与重载、跨页面冲突、提交和运行期间禁止编辑与文件操作、完成或取消清理后恢复编辑、Python 与契约修改后重跑、工作流提交视图、CFG/帧联动及无损 JSON。Temporal 测试服务器与 Playwright 浏览器首次使用时需要下载；截图位于 `playground/.browser-test/`。

后端集成测试默认下载并启动 Temporal CLI v1.6.1 的开发服务器，验证 Worker 复用、再次导入、图内并发与取消清理。设置 `AXCUTOR_TEST_TEMPORAL_ADDRESS` 可改用已有的测试专用服务器；测试使用独立执行 ID 和队列。

仅运行独立后端测试时使用 `uv run --locked pytest playground/backend/tests`。根 pytest、mypy 与 CI 已包含该包；CI 的静态检查也覆盖其源码、测试和浏览器辅助脚本。

也可设置 `AXCUTOR_BROWSER_URL=http://127.0.0.1:8080`，对已启动的 Compose 生产网页运行同一套检查。此模式会编辑目标 workspace，应使用隔离的测试项目与数据卷。
