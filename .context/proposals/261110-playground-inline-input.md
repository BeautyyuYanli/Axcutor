# Playground 运行输入（Input）交互与契约重构

## 顶部运行配置与 Input 直接编辑

移除顶部运行配置行（`.run-config`）中的 `Input file` 文件路径下拉选择器，改为绑定当前 `Entry` 契约的直接 Input JSON 编辑区。工作区文件树不再承担运行输入的存储与切换职责，中间源码编辑器（`SourceWorkspace`）专注于工作流与 Python 模块编辑。

顶部运行区行为如下：

- **Entry 选择与契约感知**：保持现有 `Entry` 下拉选择器。候选列表排除纯输入文件，仅包含工作区内的工作流定义文件（`workflows/**/__axcutor__.yaml` 及独立工作流文件）。
- **内联 Input JSON 编辑**：在 `Entry` 与 `Run` / `Cancel` 之间提供 `Input` JSON 文本输入框（支持单行紧凑展示与展开多行编辑）。当所选入口工作流的 `input` 契约为空（`{}`）时，输入框默认显示 `{}`。
- **按 Entry 维护草稿与默认骨架**：前端在内存中按 `entryPath` 维护各自的 Input JSON 草稿文本。首次选中某个 `entryPath`（或用户尚未修改过该入口草稿）时，根据 WASM 编译产物 `compileOutput.bundle.workflows[entry].input`（`Symbols`）生成初始 JSON 文本：
  - 内置示例 `workflows/demo/main/__axcutor__.yaml` 默认填充 `{"items":[3,1,2],"delay_seconds":5}`；
  - 其他工作流按顶层输入字段声明顺序递归构造默认字面量（可空类型优先使用非空基类默认值，`enum` 取首项，`integer -> 0`，`number -> 0.0`，`string -> ""`，`boolean -> false`，`array -> []`，`TypedDict` 填充必填字段，`OrderedMap -> {}`）。
  - 用户手动编辑某个 Entry 的 Input 后，切换 Entry 再切回时保留该 Entry 的已编辑文本。
- **浏览器端即时契约预检**：利用 `axcutor-wasm` 已导出的 `validate_input(input_json, schema_json)`，在编译就绪且存在 `bundle` 时对当前 Input 原始文本执行实时校验（涵盖 JSON 语法、重复键拒绝、任意精度数值与 `TypeSpec` 字段类型匹配）。校验不通过时禁用 `Run` 按钮，并在诊断区展示输入错误，无需等待后端请求往返。
- **状态锁与恢复**：在提交准备、运行中及本地清理期间（`readOnly`），Input 编辑区与源码编辑器同步设为只读。页面刷新并通过 `GET /v1/workspace/trace` 恢复最近一轮执行时，若当前 Entry 未被用户编辑且存在根调用输入记录，同步恢复对应的运行输入文本。

## 工作区文件边界与保存解耦

工作区文件集合仅管理工作流与代码资产，单次运行的 Input 文本不再作为工作区文件持久化：

- 移除示例模板中的 `examples/inputs/demo.json`。
- 修改 Input 文本不改变 `useWorkspace.dirty` 状态，也不触发工作区 `revision` 变化。
- 点击 `Run` 时：若工作区存在未保存的源码修改（`dirty === true`），先调用 `PUT /v1/workspace` 保存源码并获取新 `revision`；若源码未修改（`dirty === false`），直接携带当前 `revision` 提交运行请求，避免仅修改测试参数时的全量文件落盘与跨页面版本冲突。

## Workspace 执行接口契约

`POST /v1/workspace/executions` 的请求模型 `RunWorkspaceRequest` 将 `input` 从工作区文件路径改为与通用宿主 `POST /v1/executions`（`SubmitExecutionRequest`）一致的 JSON 对象：

```json
{
  "revision": "<workspace revision>",
  "entry": "workflows/demo/main/__axcutor__.yaml",
  "input": {
    "items": [3, 1, 2],
    "delay_seconds": 5
  }
}
```

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `revision` | string，必填 | 当前工作区 revision，不一致时返回 `409 workspace_conflict`。 |
| `entry` | string，必填 | 工作区内的相对工作流源文件路径。 |
| `input` | object，可省略 | 默认 `{}`；显式非对象返回 `400 invalid_request`，未通过入口 `TypeSpec` 校验返回 `422 input_validation_failed`。 |

后端调整范围：

- `playground/backend/src/axcutor_playground/http.py`：更新 ` openapi_schema()` 中 `RunWorkspaceRequest.properties.input` 为 `{"type": "object", "additionalProperties": True, "default": {}}`；`submit` 路由将 `body.get("input", {})` 传入 `playground.submit`。
- `playground/backend/src/axcutor_playground/coordinator.py`：`Playground.submit(entry, input, revision)` 移除对 `input` 文件路径的解析与磁盘读取，直接校验 `input` 为 `dict`，经无损 `codec.dumps` 传入 `axcutor_core.validate_input` 与 `self.executions.submit`。
- 通过 `npm --prefix playground run generate:client` 同步更新 `playground/openapi.json` 与 `playground/src/client/` 生成客户端。

## 客户端无损 JSON 传输

为保证大整数、高精度十进制数与对象键序在浏览器直接编辑并提交时不经受 `JSON.parse` / `JSON.stringify` 的精度丢失或数字键重排：

- 前端 `useExecution.run(entry: string, inputRaw: string, revision: string)` 接收原始 JSON 字符串 `inputRaw`（默认 `'{}'`）。
- 调用 `runWorkspace` 时传入自定义 `bodySerializer`，将已通过 `JsonText`（或 WASM `validate_input`）校验为合法对象的 `inputRaw` 原样嵌入请求体 JSON 字符串：
  ```ts
  bodySerializer: () => `{"entry":${JSON.stringify(entry)},"input":${inputRaw},"revision":${JSON.stringify(revision)}}`
  ```
- 后端 `read_body` 继续使用 `axcutor_runtime.codec.loads` 解析请求体，保持从前端输入框到 Python 运行时与 Temporal 的端到端无损 JSON 契约。
