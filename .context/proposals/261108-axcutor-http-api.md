# Axcutor HTTP API 与宿主入口包设计方案

## 1. 包边界与文件结构设计

CLI 与 HTTP 服务等外部入口负责从命令行或网络协议接收源码与输入、触发工作流并呈现结果，不属于 Axcutor 核心语言或确定性运行时逻辑。因此将原有的 `cli.py` 与新增的 `http.py` 从 `axcutor_runtime` 中剥离，放入独立的 `uv` Workspace 成员包 `python/axcutor_host`，并在包内抽象与传输协议无关的 `service.py` 供多协议 Controller 复用。

### 1.1 Workspace 分层职责

1. **`crates/axcutor-core` 与 `crates/axcutor-py`（编译器前端与确定性算子内核）**：纯 Rust 实现的 5-Pass 静态编译器、输入/输出 `TypeSpec` 契约校验器与 `builtin:*` 算子求值器，通过 PyO3 导出为 `axcutor_core` 原生扩展。
2. **`python/axcutor_runtime`（核心运行时库）**：纯粹的确定性解释器（`workflow.py`）、异步执行器 ABI（`activities.py`、`builtin.py`）、Worker 组装（`worker.py`）、源码包编译封装（`bundle.py`）与无损 JSON 编解码器（`codec.py`）。不包含任何命令行解析、本地文件扫描或网络协议依赖。
3. **`python/axcutor_host`（独立宿主服务与控制器包）**：依赖 `axcutor-runtime` 与 `axcutor-core`，内部划分为协议无关的宿主服务层（`service.py`）与薄协议控制器层（`cli.py`、`http.py`，以及未来可扩展的其他协议 Controller）。

### 1.2 目录与文件结构

```txt
/
├── pyproject.toml                             # uv Workspace 根配置：members = ["crates/axcutor-py", "python/axcutor_runtime", "python/axcutor_host"]
├── crates/
│   ├── axcutor-core/                          # Rust 编译器与运行时纯函数内核
│   ├── axcutor-py/                            # PyO3 绑定（构建为 axcutor_core）
│   └── axcutor-wasm/                          # WASM 绑定
│
├── python/
│   ├── axcutor_runtime/                       # 【核心运行时包：axcutor-runtime】
│   │   ├── pyproject.toml                     # 仅依赖 axcutor-core、temporalio、simplejson、pydantic（移除 [project.scripts]）
│   │   └── src/
│   │       └── axcutor_runtime/
│   │           ├── __init__.py                # 导出 AxcutorInterpreterWorkflow, compile_bundle, CompilationError
│   │           ├── activities.py              # Activity 尝试生命周期、心跳与契约校验
│   │           ├── builtin.py                 # builtin Python 执行器与代码合成
│   │           ├── bundle.py                  # 调用 axcutor_core.compile_workflow 生成自包含 bundle
│   │           ├── codec.py                   # 保持任意精度整数、Decimal 与字典键序的无损 JSON 编解码
│   │           ├── concurrency.py             # 协程组协同取消与收敛辅助函数
│   │           ├── errors.py                  # 四类可恢复异常选择器与三类引擎 Panic 映射
│   │           ├── worker.py                  # SandboxedWorkflowRunner 与 Worker 组装
│   │           └── workflow.py                # AxcutorInterpreterWorkflow 确定性图解释器与 get_runtime_snapshot 查询
│   │
│   └── axcutor_host/                          # 【独立宿主包：axcutor-host】
│       ├── pyproject.toml                     # 依赖 axcutor-runtime 与 ASGI/HTTP 库；声明 [project.scripts] axcutor = "axcutor_host.cli:main"
│       └── src/
│           └── axcutor_host/
│               ├── __init__.py                # 导出 ExecutionService 及宿主层公共异常类型
│               ├── service.py                 # 协议无关的宿主服务层（ExecutionService）：编译与输入校验、工作流调度及 Execution 状态投影
│               ├── cli.py                     # CLI Controller：本地目录源码扫描及 worker / run / serve 子命令
│               └── http.py                    # HTTP Controller：/v1/executions 路由、状态码映射与无损 JSON 响应序列化
│
└── tests/
    ├── bindings/                              # Rust/Python/WASM 绑定一致性测试
    ├── runtime/                               # 核心运行时测试（解释器、并发、超时、子工作流、执行器契约）
    └── host/                                  # 宿主服务与控制器测试
        ├── test_service.py                    # 测试 ExecutionService 编译预检与状态模型投影
        ├── test_cli.py                        # 由 tests/runtime/test_cli.py 迁入，测试 CLI 进程边界
        └── test_http.py                       # 测试 HTTP Controller 路由、状态码与错误响应契约
```

---

## 2. 宿主服务层（`service.py`）与控制器边界

`axcutor_host.service` 提供单一具体服务类 `ExecutionService`，向下封装 `axcutor_runtime`、`axcutor_core` 与 Temporal Client，向上向 `http.py`、`cli.py` 及其他协议 Controller 提供统一的领域接口，彻底屏蔽底层 Temporal 的内部概念（如 `run_id`、`history_length`、`task_queue`、Event History 阈值及 Query/Describe 拆分）。

1. **统一内存态提交与同步静态预检**：`ExecutionService` 的提交入口接收内存态 `(entry, sources, input, execution_id=None)`（本地文件系统扫描留在 `cli.py` 中构造 `sources` 字典），在内存内同步调用 `compile_bundle` 完成 5-Pass 静态编译，并调用 `axcutor_core.validate_input` 校验根工作流 `input` 契约。编译违约直接复用并抛出 `axcutor_runtime.bundle.CompilationError`（携带 `diagnostics`），输入契约违约与运行期交互异常（不存在、ID 冲突、后端不可达）映射为最小宿主异常集合，由各 Controller 转换为各自协议的错误表示（如 HTTP `422` / `404` / `409` / `503` 或 CLI 非零退出码）。
2. **运行期无状态托管与直接领域模型复用**：`ExecutionService` 本身不维护独立数据库或本地状态缓存，连接地址、命名空间、默认队列及运行时参数作为宿主初始化配置注入。其启动、查询、取消方法直接返回第 4 节定义的 `Execution` 领域结构，Controller 层直接序列化输出，不在 `service.py` 与 Controller 之间引入冗余的双重 DTO 转换层。
3. **JSON 载荷无损透传**：Axcutor 严格区分 `integer`（任意精度整数）与 `number`（十进制浮点），且要求 `OrderedMap` 严格保持键插入顺序。`service.py` 与各 Controller 对 `input`、`result.output` 以及栈帧中的 `variables` 统一采用 `axcutor_runtime.codec`（或基于 `simplejson.RawJSON` 直接内联已校验的 JSON 文本片段）进行编解码。

---

## 3. HTTP 接口总览

| 方法与路径 | 核心职责 |
| :--- | :--- |
| `POST /v1/executions` | 同步编译源码闭包、校验初始 `input` 并启动工作流执行实例。 |
| `GET /v1/executions/{execution_id}` | 获取工作流执行的当前状态视图（含生命周期状态、活跃栈帧树、终态输出或结构化异常）。 |
| `POST /v1/executions/{execution_id}:cancel` | 向在途执行发送协同取消指令，触发并发分支、子图与执行器的 `abort_signal` 清理。 |

---

## 4. 执行状态与数据模型（`Execution` 资源）

所有执行接口统一返回 `Execution` 资源对象。

### 4.1 生命周期状态（`status`）

状态枚举严格对应 Axcutor 运行时执行模型：

| 状态值 | 语义说明 | 关联字段不变量 |
| :--- | :--- | :--- |
| `running` | 工作流正在沿控制流图推进。 | `finished_at`、`result`、`error` 均为 `null`；`frames` 为当前活跃栈帧树。 |
| `completed` | 根工作流正常到达 `end` 或 `end.<branch>` 终点。 | `result` 非空（含 `branch` 与 `output`）；`frames` 为 `[]`，`error` 为 `null`。 |
| `failed` | 工作流因未捕获的可恢复异常或不可恢复引擎 Panic 终止。 | `error` 非空；`frames` 为 `[]`，`result` 为 `null`。 |
| `cancelled` | 工作流因外部取消请求完成协同取消与栈展开回滚。 | `frames` 为 `[]`，`result` 与 `error` 均为 `null`。 |

### 4.2 活跃栈帧树（`frames`）

`frames` 恒为 `Frame` 数组（终态时为 `[]`）。当 `status` 为 `running` 时，`frames` 呈现当前解释器内存中的树状激活栈帧结构（对应规范 `01-frame-memory-model.md` 与 `03-guardrails-and-step-accounting.md`），不暴露内部自增整数帧号，直接通过嵌套 `children` 表达调用与并发分叉层级：

| 字段 | 类型 | 语义说明 |
| :--- | :--- | :--- |
| `kind` | `"function" \| "block" \| "iteration"` | 栈帧类别：`function`（根工作流或 `use: workflow:<path>` 调用）、`block`（内联 `graph` 子图）、`iteration`（`for_each` 单元素迭代帧）。 |
| `address` | `string` | 栈帧在工作流中的拓扑寻址路径，如 `"main.yaml"`、`"main.yaml/sub_node"` 或 `"main.yaml/map_items[0]"`（根栈帧的 `address` 即入口工作流路径）。 |
| `cursor` | `string` | 当前控制令牌（Token）所在位置：初始为 `"start"`，执行节点中为 `"<node_id>"`（根 `run` 工作流为 `"run"`），节点完成后为 `"<node_id>.<branch>"`。 |
| `call_depth` | `integer` | 当前调用树深度（根工作流为 `1`）。 |
| `steps` | `integer` | 当前图栈帧已累计消耗的执行步数。 |
| `max_steps` | `integer \| null` | 当前图栈帧声明的步数上限护栏（`iteration` 帧与根 `run` 帧为 `null`）。 |
| `variables` | `object` | 当前栈帧作用域内所有已绑定（非 `Unbound`）变量的实时键值映射（无损保序 JSON 对象）。 |
| `children` | `array<Frame>` | 当前栈帧派生的在途子栈帧列表（如子工作流调用、内联子图、Fork-Join 并发分支或 `for_each` 并发迭代）。 |

### 4.3 终态输出（`result`）与失败详情（`error`）

* **`result`（仅 `status == "completed"` 时非空）**：
  * `branch`（`string | null`）：根工作流正常结束所走的出口分支名；默认 `end` 出口为 `null`，具名出口 `end.<branch>` 为对应分支名。
  * `output`（`object`）：根工作流在该出口分支契约下产出的输出变量字典。
* **`error`（仅 `status == "failed"` 时非空）**：
  * `category`（`"recoverable" | "panic"`）：区分逃逸至根图边界的可恢复运行时异常与不可拦截的引擎级 Panic（对应规范 `04-exception-handling-and-unwinding.md`）。
  * `type`（`string`）：
    * 当 `category == "recoverable"` 时，取值为四类标准异常选择器之一：`"execution"` | `"timeout"` | `"contract"` | `"max_steps"`；
    * 当 `category == "panic"` 时，取值为：`"StackOverflowError"` | `"MaxStepsExceededError"` | `"EngineError"`。
  * `message`（`string`）：异常的具体诊断说明。

---

## 5. 接口详细定义

### 5.1 提交并执行工作流：`POST /v1/executions`

#### 请求体（Request Body）

```json
{
  "execution_id": "order-flow-20261108-001",
  "entry": "main.yaml",
  "sources": {
    "main.yaml": {
      "format": "yaml",
      "text": "input:\n  items: { type: array, items: { type: integer } }\noutput:\n  doubled: { type: array, items: { type: integer } }\n  total: { type: integer }\n..."
    },
    "double.yaml": {
      "format": "yaml",
      "text": "..."
    }
  },
  "input": {
    "items": [3, 1, 2]
  }
}
```

* `execution_id`（可选，`string`）：客户端指定的幂等执行标识；省略时由服务端自动生成 UUID。若存在同名冲突执行，返回 `409 Conflict`。
* `entry`（必填，`string`）：入口工作流在 `sources` 中的相对 POSIX 路径键名。
* `sources`（必填，`map<string, {format: "yaml" | "json", text: string}>`）：工作流源码快照集合，包含入口工作流及其传递引用的全部 `use: workflow:<path>` 文档。
* `input`（可选，`object`，默认 `{}`）：根工作流初始输入对象。

#### 响应体（Response `202 Accepted`）

同步编译与输入校验通过并启动执行后，返回 `202 Accepted`（附带 `Location: /v1/executions/{execution_id}` 响应头）及初始 `Execution` 资源对象：

```json
{
  "execution_id": "order-flow-20261108-001",
  "status": "running",
  "started_at": "2026-11-08T05:40:00Z",
  "finished_at": null,
  "frames": [],
  "result": null,
  "error": null
}
```

---

### 5.2 查询执行状态与结果：`GET /v1/executions/{execution_id}`

#### 响应示例（`200 OK` - 运行中）

```json
{
  "execution_id": "order-flow-20261108-001",
  "status": "running",
  "started_at": "2026-11-08T05:40:00Z",
  "finished_at": null,
  "frames": [
    {
      "kind": "function",
      "address": "main.yaml",
      "cursor": "map_items",
      "call_depth": 1,
      "steps": 2,
      "max_steps": 1000,
      "variables": {
        "items": [3, 1, 2]
      },
      "children": [
        {
          "kind": "iteration",
          "address": "main.yaml/map_items[0]",
          "cursor": "double_call",
          "call_depth": 1,
          "steps": 0,
          "max_steps": null,
          "variables": {
            "items": [3, 1, 2],
            "item": 3
          },
          "children": [
            {
              "kind": "function",
              "address": "main.yaml/map_items[0]/double_call",
              "cursor": "run",
              "call_depth": 2,
              "steps": 0,
              "max_steps": null,
              "variables": {
                "x": 3
              },
              "children": []
            }
          ]
        }
      ]
    }
  ],
  "result": null,
  "error": null
}
```

#### 响应示例（`200 OK` - 正常完成）

```json
{
  "execution_id": "order-flow-20261108-001",
  "status": "completed",
  "started_at": "2026-11-08T05:40:00Z",
  "finished_at": "2026-11-08T05:40:05Z",
  "frames": [],
  "result": {
    "branch": null,
    "output": {
      "doubled": [6, 2, 4],
      "total": 12
    }
  },
  "error": null
}
```

#### 响应示例（`200 OK` - 异常终止）

```json
{
  "execution_id": "order-flow-20261108-002",
  "status": "failed",
  "started_at": "2026-11-08T05:41:00Z",
  "finished_at": "2026-11-08T05:41:02Z",
  "frames": [],
  "result": null,
  "error": {
    "category": "recoverable",
    "type": "contract",
    "message": "missing required output property 'total'"
  }
}
```

---

### 5.3 协同取消执行：`POST /v1/executions/{execution_id}:cancel`

向指定执行发送取消信号，触发解释器沿活跃栈帧树广播 `CancelledError` 并通过 `abort_signal` 终止在途 `run` 执行器。

* 若执行尚在清理在途任务，返回 `202 Accepted` 及当前 `Execution` 状态；
* 若执行已完成取消或已处于终态（`completed` / `failed` / `cancelled`），幂等返回 `200 OK` 及当前终态 `Execution` 对象。

---

## 6. 错误响应契约

所有非 `2xx` 响应统一采用 `{ "error": { "code": "<string>", "message": "<string>", "details": <any> } }` 结构：

| HTTP 状态码 | `error.code` | 触发场景与 `details` 结构 |
| :--- | :--- | :--- |
| `400 Bad Request` | `invalid_json` | 请求体不是合法 JSON、存在重复对象键或包含非有限数值（`NaN` / `Infinity`）。 |
| `400 Bad Request` | `invalid_request` | 缺少必填字段（如 `entry`、`sources`）或字段类型不匹配。 |
| `404 Not Found` | `execution_not_found` | 指定的 `execution_id` 不存在。 |
| `409 Conflict` | `execution_already_exists` | 提交时指定的 `execution_id` 已存在冲突执行。 |
| `422 Unprocessable Entity` | `compilation_failed` | 工作流源码未通过 5-Pass 静态检查，`details` 为 `{ "diagnostics": Diagnostic[] }`。 |
| `422 Unprocessable Entity` | `input_validation_failed` | `input` 不符合入口工作流 `input` 的 `TypeSpec` 契约。 |
| `503 Service Unavailable` | `runtime_unavailable` | 后端运行时服务不可达或无可用 Worker 响应。 |
