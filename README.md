# Axcutor 编译器与 Temporal 运行时

Rust workspace 实现 [RFC 11](rfc/0011-compiler-and-temporal-runtime-architecture.md) 的编译器前端：严格 YAML/JSON 解析、显式符号与类型检查、16 个 builtin 静态契约、拓扑与并发写入检查、分支敏感必达定值，以及画布端口查询和变量补全。语言依据为 [manual/spec/01–03](manual/spec/index.md)，实施范围见[定稿提案](.context/proposals/261107-rfc11-compiler-frontend.md)。

`axcutor-core` 无文件或网络 I/O。调用方一次性提供入口和源文档快照；编译器仅检查入口的递归依赖闭包。成功返回拥有独立存储的归一化 AST bundle；任何错误都会使 `bundle` 为 `null`。

运行时由 Rust 纯函数内核、PyO3 文本绑定和 Python Temporal 解释器组成，支持 16 个 builtin、隔离帧、分支、重试与捕获、结构化并发、调用护栏和状态 Query。业务执行器注册为异步 Python 函数，通过标准 `execute_run` Activity 接入。请求、选项和 bundle 随 Temporal History 保存；解释器无需独立数据库。公开接口、Worker 示例与生命周期约束见 [运行时说明](docs/runtime-api.md)。

## 调用

```rust
use axcutor_core::{CompileRequest, compile_workflow};

let request: CompileRequest = serde_json::from_str(r#"{
  "entry": "main",
  "sources": {
    "main": {"format":"yaml", "text":"input: {}\noutput: {}\nrun: work\n"}
  }
}"#).expect("valid request");
let output = compile_workflow(request);
assert!(output.bundle.is_some());
```

WASM 和 Python 使用相同的 JSON 文本接口：

```text
compile_workflow(request_json) -> result_json
get_available_vars_at_node(analysis_json, node_id, expected_json?) -> candidates_json
derive_node_ports(analysis_json, node_id) -> ports_json
```

其中 `analysis_json` 是编译结果 `analysis` 中一张图的完整 `GraphAnalysis`。工作流和内联图通过 `workflow_id`、`scope_graph_path` 区分。Python 模块名为 `axcutor_core`；参数错误抛出 `ValueError`，WASM 抛出 JS `Error`；源文档中的语言错误返回结构化诊断。完整数据模型和约束见 [接口说明](docs/compiler-api.md)。

数字保持整数/浮点区别和任意精度十进制值，映射保持声明顺序，`default` 缺省与 `default: null` 不同。应保存原始结果文本；提取图分析时使用能够保留数字和映射顺序的 JSON 处理器。JS 的 `JSON.parse` / `JSON.stringify` 往返会破坏大整数、精细小数及数字样式键的顺序，不能用来重新发布 bundle。

## 检查与构建

需要 Rust/Cargo（声明的最低版本为 1.85）、Python 3.10+、Node，以及匹配 crate 版本的 wasm-bindgen CLI；浏览器检查另需 Chrome/Chromium。所有命令从仓库根目录执行。

Temporal 运行时最低 Python 3.11。uv workspace 会构建真实的本地扩展；测试使用真实 Temporal sandbox 和 time-skipping Server，首次执行需要下载测试服务：

```sh
uv sync --locked --python 3.12
uv run --locked ruff check python tests/runtime
uv run --locked ruff format --check python tests/runtime
uv run --locked mypy
uv run --locked pytest tests/runtime
```

```sh
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cargo build --workspace --release --locked
cargo run --quiet -p axcutor-core --example binding_oracle -- tests/bindings/cases.json target/bindings-oracle.json
```

Python 扩展可通过独立虚拟环境构建并真实调用：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'maturin>=1.9,<2'
.venv/bin/maturin build --locked --release --manifest-path crates/axcutor-py/Cargo.toml --interpreter .venv/bin/python --out target/wheels
.venv/bin/python -m pip install --force-reinstall target/wheels/*.whl
.venv/bin/python tests/bindings/python_smoke.py target/bindings-oracle.json
```

WASM 检查构建真实的 `wasm32-unknown-unknown` 产物，再分别在 Node 和浏览器执行：

```sh
rustup target add wasm32-unknown-unknown
cargo install wasm-bindgen-cli --version 0.2.129 --locked
cargo build --locked --release -p axcutor-wasm --target wasm32-unknown-unknown
wasm-bindgen --target nodejs --out-dir target/wasm-node target/wasm32-unknown-unknown/release/axcutor_wasm.wasm
wasm-bindgen --target web --out-dir target/wasm-web target/wasm32-unknown-unknown/release/axcutor_wasm.wasm
node tests/bindings/wasm_smoke.cjs target/wasm-node/axcutor_wasm.js target/bindings-oracle.json
python3 tests/bindings/browser_smoke.py
```

浏览器脚本从维护的 HTML 模板生成临时页面，将生成的 web glue、WASM 和原生 oracle 嵌入后执行；不依赖本机 HTTP 服务。可用 `AXCUTOR_CHROME=/path/to/chrome` 指定浏览器二进制。`Cargo.lock` 由 Cargo 生成；wheel、glue、WASM、oracle 和临时 HTML 均由对应工具生成，不维护生成物。

测试覆盖全部 16 个 builtin 的正反契约、类型与语法、递归闭包、多错误和定位、环路不动点、分支 kill、Fork 同快照与写集合隔离、三类迭代边界、补全与端口。17 个完整自包含的 guide/cookbook 示例按原文编译；片段、占位符和缺失外部定义的例子不作为完整合法输入。手册中的显式反例保持拒绝。
