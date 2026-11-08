# Axcutor Web Demo 前端设计方案

## 1. 架构定位与职责边界

Web 前端定位为单页演练与调试工作台（Playground），直接组合浏览器端 `axcutor-wasm` 静态编译器内核与 `axcutor-host` 暴露的 `/v1/executions` HTTP 接口，不在后端引入额外业务路由、WebSocket 或持久化状态。

### 1.1 前后端交互边界

1. **静态编译与画布分析（浏览器本地 `axcutor-wasm`）**：
   * 源码闭包编辑、5-Pass 静态检查与节点入口可用变量推导（`get_available_vars_at_node`）全部在浏览器内通过 WebAssembly 同步完成。
   * 每次编辑源码闭包（`sources`）或切换入口路径（`entry`），前端构造 `CompileRequest` JSON 文本调用 `compile_workflow(request_json)`，直接消费返回的 `CompileOutput = { diagnostics, analysis, bundle }`（其中节点端口直接读取 `GraphAnalysis.nodes[nodeId].ports`）。
2. **动态执行与状态投影（`axcutor-host` OpenAPI + `@hey-api/openapi-ts` Client）**：
   * 宿主侧在 `axcutor_host/http.py` 维护与 `/v1/executions` 路由契约一致的 OpenAPI 3.1 Schema（通过 `GET /openapi.json` 暴露或导出静态 Schema），前端通过 `@hey-api/openapi-ts` 自动生成强类型客户端与数据模型至 `playground/src/client/`。
   * 提交执行：调用生成的 `submitExecution`（`POST /v1/executions`），请求体传入 `{ entry, sources, input }`，由服务端生成 `execution_id`。
   * 状态轮询：调用生成的 `getExecution`（`GET /v1/executions/{execution_id}`）。当 `status === "running"` 时以 `300ms` 间隔轮询，进入 `completed`、`failed`、`cancelled` 任一终态时立即停止。
   * 协同取消：调用生成的 `cancelExecution`（`POST /v1/executions/{execution_id}:cancel`），在 `202 Accepted`（清理中，`status: "running"`）期间继续轮询，直至收敛为 `200 OK` 终态。

### 1.2 无损 JSON 载荷与 `@hey-api/openapi-ts` 集成契约

Axcutor 严格区分任意精度 `integer` 与十进制 `number`（如 `1` 与 `1.0`），并要求对象键严格保序且无重复键。JavaScript 原生 `JSON.parse` / `JSON.stringify` 会将 `1.0` 折叠为 `1`、将超大整数截断为 IEEE-754 双精度浮点，并重排非负整数样式的字符串键。前端在生成的 Hey API Client 上通过自定义 `bodySerializer` 与原始 JSON 子串提取保持无损契约：

1. **编译器调用**：传给 `axcutor-wasm` 的 `CompileRequest` 仅包含字符串字典 `sources[path] = { format, text }` 与 `entry`，不涉及用户业务数值反序列化。
2. **提交请求体序列化（自定义 `bodySerializer`）**：用户在 Input 编辑器中编写的 JSON 文本 `inputRawText` 以原始文本持有；调用 `submitExecution` 时，通过 Hey API 请求配置的 `bodySerializer` 将 `inputRawText` 作为原始 JSON 片段直接拼接到 `{ entry, sources }` 信封中，避免对用户输入执行 `JSON.parse` + `JSON.stringify`：
   ```ts
   const serializeSubmitBody = (body: {
     entry: string;
     sources: Record<string, SourceDocument>;
     inputRawText: string;
   }): string => {
     const prefix = JSON.stringify({ entry: body.entry, sources: body.sources }).slice(0, -1);
     return `${prefix},"input":${body.inputRawText.trim() || "{}"}}`;
   };
   ```
3. **响应报文解析与无损原文展示**：Hey API Client 保留响应原始 JSON 文本 `rawText`；前端通过轻量 JSON token 切片工具按路径直接提取 `frames[i].variables` 与 `result.output` 的原始 JSON 子串并缩进格式化，在 `<pre><code>` 中原样渲染（100% 保留整数/小数尾数格式与字典键顺序），其余元数据字段（`execution_id`、`status`、`started_at`、`finished_at`、`error`、`frames` 拓扑字段）直接映射为 Hey API 生成的 TypeScript 接口类型。

### 1.3 极简无设计（Unstyled / Structural-Only CSS）规范

前端界面严格遵循“极简无设计”原则，尽量少写样式，为后续接入视觉设计预留空间：

1. **仅写结构布局样式**：CSS 仅用于工作台主分栏（CSS Grid / Flexbox）与 `@vue-flow/core` 画布容器的必要宽高尺寸（`width: 100%; height: 100%`），画布仅引入功能性结构样式 `@vue-flow/core/dist/style.css`（不引入 `@vue-flow/core/dist/theme-default.css` 默认主题）。
2. **原生语义化 HTML 控件**：所有交互与展示直接采用浏览器原生 `<button>`、`<select>`、`<input>`、`<textarea>`、`<details>` / `<summary>`、`<table>`、`<fieldset>` 与 `<pre><code>`，不引入 UI 组件库或图标库。
3. **零装饰与语义属性钩子**：不编写阴影、圆角、渐变、配色方案或自定义 CSS 动画；组件与画布元素统一通过 `data-status`、`data-node-kind`、`data-port-kind="normal | catch"`、`data-active` 等语义化属性钩子表达状态，运行态活跃连边直接使用 Vue Flow 内置的 `animated: true` 属性。

---

## 2. 目录结构与技术栈

前端工程位于仓库根目录下的 `playground/`，采用 **Vite + Vue 3（`<script setup lang="ts">`）+ TypeScript + `@hey-api/openapi-ts` + `@vue-flow/core` + `@dagrejs/dagre`** 构建，直接基于 Vue 响应式 `ref` / `shallowRef` / `computed` 管理状态。

### 2.1 目录结构

```txt
/
├── crates/
│   └── axcutor-wasm/                          # Rust WASM 绑定（输出至 playground/src/wasm-pkg/）
├── python/
│   └── axcutor_host/
│       └── src/axcutor_host/
│           └── http.py                        # /v1/executions 路由及 OpenAPI 3.1 Schema 定义/导出
└── playground/
    ├── index.html
    ├── openapi-ts.config.ts                   # @hey-api/openapi-ts 配置文件
    ├── package.json                           # 依赖 vue, @hey-api/client-fetch, @vue-flow/core, @dagrejs/dagre
    ├── tsconfig.json
    ├── vite.config.ts                         # 配置 /v1 代理至 127.0.0.1:8000
    └── src/
        ├── main.ts
        ├── App.vue                            # 主工作台 Grid 布局与顶栏控制
        ├── wasm-pkg/                          # wasm-bindgen --target web 生成的胶水代码与 .wasm
        ├── client/                            # @hey-api/openapi-ts 自动生成的 SDK 与 types.gen.ts
        ├── types/
        │   └── compiler.ts                    # WASM 侧 CompileRequest, CompileOutput, Diagnostic, GraphAnalysis, NormalizedAst
        ├── utils/
        │   ├── losslessJson.ts                # 请求体 raw input 拼接与响应 variables/output 原始 JSON 子串提取工具
        │   └── graphLayout.ts                 # 将 NormalizedRoot + GraphAnalysis 转换为 Vue Flow Node[]/Edge[] 并经 Dagre 计算坐标
        ├── composables/
        │   ├── useCompiler.ts                 # 封装 axcutor-wasm 初始化、响应式编译与 get_available_vars_at_node 查询
        │   └── useExecution.ts                # 封装 Hey API Client 调用（submit/get/cancel）与 300ms 轮询定时器
        ├── presets/
        │   └── index.ts                       # 内置默认工作流快照（复用 examples/workflows/main.yaml + double.yaml）
        └── components/
            ├── SourceWorkspace.vue            # 多文件源码切换、entry 选择与 Input 原始 JSON <textarea>
            ├── DiagnosticsPanel.vue           # 编译诊断与 HTTP 错误原生表格列表（支持点击定位文件/节点）
            ├── CfgCanvas.vue                  # 基于 @vue-flow/core 的控制流图画布、多 <Handle> 节点与静态探针面板
            └── ExecutionInspector.vue         # 实时执行状态、Frame 活跃栈帧树与无损 JSON 原文 <pre> 视图
```

### 2.2 构建与客户端生成配置

1. **宿主 OpenAPI Schema 与 Hey API 客户端生成**：
   在 `axcutor_host/http.py` 中提供 OpenAPI 3.1 描述字典（涵盖 `POST /v1/executions`、`GET /v1/executions/{execution_id}`、`POST /v1/executions/{execution_id}:cancel` 及 `Execution`、`Frame`、`ExecutionResult`、`ExecutionError`、`ErrorResponse` 组件 Schema），并在 `playground/openapi-ts.config.ts` 中配置：
   ```ts
   import { defineConfig } from "@hey-api/openapi-ts";

   export default defineConfig({
     client: "@hey-api/client-fetch",
     input: "./openapi.json",
     output: "src/client",
   });
   ```
2. **WASM 产物生成**：
   ```sh
   cargo build --locked --release -p axcutor-wasm --target wasm32-unknown-unknown
   wasm-bindgen --target web --out-dir playground/src/wasm-pkg target/wasm32-unknown-unknown/release/axcutor_wasm.wasm
   ```
3. **Vite 开发代理（`playground/vite.config.ts`）**：
   将 `/v1` 代理至 `axcutor serve` 默认监听的 `http://127.0.0.1:8000`：
   ```ts
   import { defineConfig } from "vite";
   import vue from "@vitejs/plugin-vue";

   export default defineConfig({
     plugins: [vue()],
     server: {
       port: 5173,
       proxy: {
         "/v1": {
           target: "http://127.0.0.1:8000",
           changeOrigin: true,
         },
       },
     },
   });
   ```

---

## 3. 核心状态模型与接口定义

### 3.1 编译器与画布分析类型（`playground/src/types/compiler.ts`）

对齐 `crates/axcutor-core/src/compiler/mod.rs` 与 `crates/axcutor-core/src/ast/mod.rs` 的序列化结构：

```ts
export type SourceFormat = "yaml" | "json";

export interface SourceDocument {
  format: SourceFormat;
  text: string;
}

export interface CompileRequest {
  entry: string;
  sources: Record<string, SourceDocument>;
}

export interface SourceLocation {
  line: number;
  column: number;
}

export interface Diagnostic {
  workflow_id: string;
  scope_graph_path: string[];
  target_kind: "document" | "node" | "edge" | "port" | "variable" | string;
  node_id: string | null;
  branch_id: string | null;
  edge_index: number | null;
  param_path: string;
  code: string;
  message: string;
  source_location: SourceLocation | null;
}

export interface NodePort {
  branch_id: string | null;
  kind: "normal" | "catch";
  output: string[];
  kill_vars: string[];
}

export interface NodeAnalysis {
  definite_in: string[] | null;
  injected: Record<string, TypeSpec>;
  ports: NodePort[] | null;
}

export interface GraphAnalysis {
  workflow_id: string;
  scope_graph_path: string[];
  symbols: Record<string, TypeSpec>;
  nodes: Record<string, NodeAnalysis>;
}

export interface FieldPath {
  root: string;
  fields: string[];
}

export interface VariableCandidate {
  path: FieldPath;
  typespec: TypeSpec;
}

export interface TypeSpec {
  type: string;
  nullable?: boolean;
  items?: TypeSpec;
  properties?: Record<string, TypeSpec>;
  required?: string[];
  additionalProperties?: TypeSpec;
  enum?: unknown[];
}

export interface CompileOutput {
  diagnostics: Diagnostic[];
  analysis: GraphAnalysis[];
  bundle: WorkflowBundle | null;
}
```

### 3.2 执行模型（`playground/src/client/types.gen.ts`）与无损原文切片（`playground/src/utils/losslessJson.ts`）

`Execution`、`Frame`、`ExecutionResult`、`ExecutionError` 与 `ErrorResponse` 由 `@hey-api/openapi-ts` 从宿主 OpenAPI Schema 自动生成；`losslessJson.ts` 提供从响应原始 JSON 字符串中按拓扑路径提取无损原文子串的辅助结构：

```ts
export interface RawExecutionSlices {
  /** 按帧在先序遍历中的索引或 address 映射原始 variables JSON 文本 */
  frameVariablesRaw: Map<string, string>;
  /** 终态 result.output 的原始 JSON 文本 */
  resultOutputRaw: string | null;
}
```

---

## 4. 分阶段模块设计

### 4.1 第一阶段：WASM 静态编译与 HTTP 执行闭环

#### 4.1.1 静态编译器 Composable（`useCompiler.ts`）

* **生命周期**：应用挂载时调用 `init()` 加载 `axcutor_wasm_bg.wasm`。
* **实时编译**：监听 `entry` 与 `sources` 变化，同步调用 `compile_workflow(JSON.stringify({ entry, sources }))`，更新 `compileOutput: shallowRef<CompileOutput | null>`。
* **单节点可用变量查询封装**：
  * 在用户选中画布单个节点时，按需调用 WASM `get_available_vars_at_node(JSON.stringify(analysis), nodeId, expectedTypeJson)` 获取当前节点入口满足可选目标 `TypeSpec` 的变量与字段投影路径。

#### 4.1.2 多文件源码与 Input 工作区（`SourceWorkspace.vue`）

* **文件字典管理**：维护 `sources: Record<string, SourceDocument>`，支持加载默认预设工作流、新增/删除文件（选择 `yaml` 或 `json` 格式并输入路径名）以及切换 `entry`；路径与语法合法性直接交由 `axcutor-wasm` 与 `axcutor-host` 校验并返回诊断。
* **入口契约提示**：当 `bundle?.workflows[entry]` 存在时，在 Input `<textarea>` 上方列出入口声明的 `input` 字段名及其 `TypeSpec` 摘要，辅助用户编写合法的 `inputRawText`。
* **诊断联动**：在每个文件切换按钮上显示属于该 `workflow_id` 的诊断数量；点击底部 `DiagnosticsPanel.vue` 中的任一诊断行，自动切换到对应 `workflow_id` 并选中对应 `node_id`。

#### 4.1.3 执行控制器 Composable（`useExecution.ts`）与右侧监视器（`ExecutionInspector.vue`）

* **提交与状态轮询**：
  * 点击“Run”时，通过 Hey API 生成的 `submitExecution` 提交 `{ entry, sources, input }`（使用自定义 `bodySerializer` 内联 `inputRawText`）。
  * 若返回 `202 Accepted`，保存返回的 `execution` 对象及 `RawExecutionSlices`；若 `status === "running"`，启动 `300ms` 定时器调用 `getExecution({ path: { execution_id } })`，直至进入终态。
  * 若返回非 `2xx` 错误（如 `422 compilation_failed`、`422 input_validation_failed`、`400`、`503`），直接解析生成的错误信封展示在诊断面板或错误区域中。
* **协同取消**：
  * 在 `status === "running"` 期间点击“Cancel”，调用生成的 `cancelExecution({ path: { execution_id } })` 并将按钮置为禁用状态，保持轮询直至 `status` 变为终态。
* **栈帧树与无损变量渲染（`ExecutionInspector.vue`）**：
  * 使用原生 `<details>` 递归渲染 `execution.frames`：每帧显示 `kind`（`function` / `block` / `iteration`）、`address`、`cursor`、`call_depth`、`steps / max_steps`，并在 `<pre><code>` 中直接输出 `frameVariablesRaw` 对应的无损 JSON 原文。
  * 终态区域：`completed` 时展示出口分支 `branch ?? "end"` 与 `<pre><code>` 承载的 `resultOutputRaw`；`failed` 时展示 `error.category`（`recoverable` / `panic`）、`error.type` 和 `error.message`。

---

### 4.2 第二阶段：基于 `@vue-flow/core` 的 CFG 画布与运行态游标叠加

#### 4.2.1 作用域图导航与 Vue Flow 拓扑转换（`graphLayout.ts` & `CfgCanvas.vue`）

* **作用域寻址（Scope Navigation）**：
  * 画布当前展示的图由 `(workflowId: string, scopeGraphPath: string[])` 唯一标识，顶部提供原生按钮面包屑导航（如 `main.yaml` / `inline_block`）。
  * 若目标 `NormalizedRoot.body.kind === "run"`（单节点 Root Run 工作流，如 `double.yaml`），生成 `start -> run -> end` 三个 Vue Flow `Node` 与两条 `Edge`；若为 `graph`（`NormalizedGraph`），则提取其 `nodes` 与 `routes` 转换为 Vue Flow 的 `Node[]` 与 `Edge[]`，并调用 `@dagrejs/dagre`（`rankdir: "TB"`）自动计算每个 `Node` 的 `position: { x, y }`。
* **自定义节点、多 `<Handle>` 端口与连边映射**：
  * **节点模板与属性钩子**：在 `CfgCanvas.vue` 中注册极简自定义节点插槽（仅含 `<fieldset>`、节点 ID、`NormalizedNode.body.kind` 与 `for_each` / `retry` / `timeout_ms` 文本标签），挂载 `data-node-kind` 与 `data-active` 属性钩子；双击 `workflow` 节点切换到目标 `workflow_id`，双击 `graph` 内联子图节点下钻进入 `[...scopeGraphPath, nodeId]`。
  * **多 `<Handle>` 端口渲染**：每个节点直接读取 `GraphAnalysis.nodes[nodeId].ports ?? []`，在节点底部循环渲染 `@vue-flow/core` 的 `<Handle type="source" :id="port.branch_id ?? 'default'" :data-port-kind="port.kind" />`，端口定位与连线附着完全由 Vue Flow 处理。
  * **连边与 Fork-Join 映射**：遍历 `NormalizedGraph.routes` 生成 Vue Flow `Edge[]`：
    * `Direct` 边：`source` 与 `sourceHandle` 对应源节点与分支端口，连向目标节点、`end`、`end.<branch>` 或 `fail`；`label` 拼接分支名与非空 `kill_vars`（如 `none (kill: x)`）。
    * `ForkJoin` 边（`source -> [w1, w2] -> join_target`）：展开为从源端口到各 `worker` 的分叉 `Edge` 以及从各 `worker` 到 `join_target` 的汇合 `Edge`。

#### 4.2.2 节点静态探针（Canvas Intelligence Inspector）

* 监听 Vue Flow 的 `@node-click` 事件，选中节点 `nodeId` 时在画布侧栏展示静态编译证明信息：
  1. **入口必达定值集合（`definite_in`）**：读取 `GraphAnalysis.nodes[nodeId].definite_in` 与 `injected`（如 `for_each` 注入的 `item`、`key`、`index`），在原生 `<table>` 中列出到达该节点时必达定值的变量及其 `TypeSpec`。
  2. **上下文可用变量与子类型过滤演示**：调用 WASM `get_available_vars_at_node(analysisJson, nodeId, expectedTypeJson)`，列出所有合法的顶层变量与 `.field` 投影路径；提供原生 `<select>` 切换期望类型（`All`、`integer`、`number`、`string`、`boolean`），验证编译器的协变子类型过滤能力。
  3. **派生端口契约**：读取 `GraphAnalysis.nodes[nodeId].ports ?? []`，展示每个端口承诺写入的 `output` 变量集与抹除的 `kill_vars` 集合。

#### 4.2.3 运行态实时游标叠加（Live Token Overlay）

* 当 `execution?.status === "running"` 时，将 `execution.frames` 实时映射为当前画布 `(workflowId, scopeGraphPath)` 上 Vue Flow `Node` 与 `Edge` 的状态属性：
  * **地址解析与作用域匹配**：遍历 `frames` 树中的每个 `Frame`，根据其拓扑 `address`（如 `main.yaml`、`main.yaml/double[0]`、`main.yaml/double[0]/double`）关联到对应的 `(workflowId, scopeGraphPath)` 及当前所属节点。
  * **活跃节点与连边标记**：
    * 若 `frame.cursor === "start"` 或 `"<node_id>"`（或 Root Run 的 `"run"`），将对应 Vue Flow 节点的 `data-active="true"` 置为激活（仅用基础实线边框区分）；
    * 若 `frame.cursor === "<node_id>.<branch>"`，将从该分支端口发出的 Vue Flow `Edge` 设为 `animated: true`；
    * 若某节点下挂有多个 `kind === "iteration"` 的并发子帧，在节点文本中附加在途迭代计数（如 `running: [0], [1]`）。
  * **栈帧与画布双向联动**：在右侧 `ExecutionInspector.vue` 中点击任意 `Frame` 项，自动将中部画布切换到该栈帧所在的 `(workflowId, scopeGraphPath)`。
