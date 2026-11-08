# Web Playground

Playground 在浏览器中使用 WASM 编译源码，通过现有 Execution HTTP API 提交、轮询与取消执行。源码与 Input 保存在当前页面内存中；刷新页面会加载仓库默认示例。执行状态与历史由 Temporal 保存。

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

`generate:client` 调用 `python -m axcutor_host.http --output openapi.json`，随后用固定版本的 Hey API 生成 `playground/src/client/`。当前 Hey API 将 Fetch client 随生成器内置，因此不另安装已弃用的 `@hey-api/client-fetch` 包。

`generate:wasm` 使用 `cargo build --locked --release -p axcutor-wasm --target wasm32-unknown-unknown` 与 `wasm-bindgen --target web` 生成 `playground/src/wasm-pkg/`。脚本从仓库 `Cargo.toml` 读取绑定版本并核对 CLI；可通过 `AXCUTOR_CARGO`、`AXCUTOR_WASM_BINDGEN` 指定可执行文件，支持 `CARGO_TARGET_DIR`。生成的 WASM 与胶水代码不纳入 Git，首次运行前必须生成；修改 Rust 后应重新运行该命令。OpenAPI 和 SDK 纳入 Git，修改宿主协议后应重新生成，不手工编辑产物。

## 启动

```sh
# 启动 Temporal、Worker 和 HTTP API，默认 API 端口为 8000。
docker compose up -d --build

# 启动开发页面；访问终端输出的 http://127.0.0.1:5173/。
npm --prefix playground run dev
```

已有本机 Temporal 时，可分别运行 `uv run --locked axcutor worker` 与 `uv run --locked axcutor serve` 代替 Docker 宿主。Vite 默认将 `/v1` 和 `/openapi.json` 代理至 `http://127.0.0.1:8000`；更换 API 地址时使用：

```sh
AXCUTOR_API_URL=http://127.0.0.1:9000 npm --prefix playground run dev
```

生产静态文件位于 `playground/dist/`。部署时需由同一站点将 `/v1` 转发至 HTTP 宿主；`vite preview` 仅用于预览静态构建，不代替该 API 转发配置。

## 编辑、检查与执行

1. 在 Sources 中选择文件，编辑原始 YAML/JSON，或添加、删除文件。Entry 选择编译入口；未被入口闭包引用的文件不参与编译。默认预设直接读取 `examples/workflows/main.yaml`、`double.yaml` 和 `examples/inputs/demo.json`。
2. 编译自动在浏览器内执行。诊断显示文件、作用域、节点与参数位置；点击诊断可切换到对应源码和节点。Input 上方可展开入口的字段契约。
3. 点击 CFG 节点查看 `definite_in`、注入项、派生端口及可用变量投影；Expected type 选择器演示 WASM 的子类型过滤。双击 workflow/graph 节点或点击 Open 按钮进入目标工作流/内联图，面包屑返回上层作用域。
4. 点击 Run 提交。运行中每隔 300ms 查询一次；右侧展示活跃帧的地址、游标、调用深度、步数与变量。点击帧可跳转到对应 CFG；在执行快照中点击节点也会选中对应活跃帧。并发节点显示在途迭代索引，活跃节点和连边暴露语义状态属性。
5. Cancel 发起协同取消。服务端返回 `202 / running` 表示仍在清理，页面继续轮询并禁用重复取消；进入 `completed`、`failed` 或 `cancelled` 时停止轮询。

Canvas source 的 Submitted snapshot 始终对应提交时的编译结果。运行中编辑源码会切换到 Editor compilation；点击帧可返回执行快照，避免当前源码与在途执行的拓扑混用。离开页面只停止观察，不会取消已提交的执行。

## 无损 JSON

Input 以原始 JSON 文本保存，通过自定义请求序列化器直接拼入信封。以下数值及对象键序不会经 JavaScript 浮点数或对象枚举转换：

```json
{"integer":99999999999999999999999999999999999,"number":1.0,"ordered":{"2":2,"1":1,"z":3}}
```

重复对象键、非有限数值、非法 JSON 和非对象 Input 会在提交前报错。响应中的 `frames[].variables`、`result.output` 和错误详情按原始 token 提取并仅调整空白。宿主可规范化等价的十进制写法，数值与 integer/number 类别遵循 [HTTP API 无损 JSON 契约](../reference/04-execution-http-api.md)。编译结果中的原始 analysis 也直接交回 WASM，保留 TypeSpec 枚举的数值与键序。

## 验证

```sh
# TypeScript、生产构建、真实 WASM 与客户端边界测试。
npm --prefix playground run build
npm --prefix playground test

# 使用真实 HTTP、Worker 与临时 Temporal Server 执行 Chromium 测试。
# 可设置 AXCUTOR_CHROME 指向已安装的 Chrome/Chromium。
npm --prefix playground exec -- playwright install chromium
npm --prefix playground run test:browser

# 宿主路由与 Schema 契约。
uv run --locked pytest tests/host/test_http.py
uv run --locked ruff check python/axcutor_host/src tests/host/test_http.py playground/scripts
uv run --locked mypy
```

浏览器测试会选择临时 HTTP 与页面端口，结束时关闭测试 Worker 和 Server，不依赖已有开发服务。Temporal 测试服务器与 Playwright 浏览器首次使用时需要下载；测试截图生成至被 Git 忽略的 `playground/.browser-test/`。
