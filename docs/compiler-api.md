# 编译与画布接口（0.1）

本接口处理一次不可变的源快照。核心不加载路径、不调用宿主 resolver、不保存缓存；结果中的字符串、类型、常量、图和依赖均由返回值拥有。三个宿主入口使用同一个 Rust 核心。

## 编译请求与输出

`CompileRequest = { entry: String, sources: Map<String, SourceDocument> }`。`SourceDocument = { format: "yaml" | "json", text: String }`。资源名只要求非空，`workflow:` 后缀按完整字符串精确匹配，不做相对路径或文件系统解析。依赖按发现顺序遍历，先收集闭包中的全部顶层签名，再检查实现；支持直接和间接递归，不展开被调用图体。未引用的源文本不会解析。

```json
{
  "diagnostics": [],
  "analysis": [],
  "bundle": {
    "entry": "main",
    "workflows": {}
  }
}
```

上例只展示结果字段形状；实际成功结果的 `workflows` 必须含入口及所有传递依赖，根 run 没有图分析。任何闭包错误都使 `bundle: null`。不通过空签名、空类型、占位节点或补默认路由容忍无效程序。

Rust 接口：

```rust
pub fn compile_workflow(request: CompileRequest) -> CompileOutput;
pub fn get_available_vars_at_node(
    analysis: &GraphAnalysis,
    node_id: &str,
    expected: Option<&TypeSpec>,
) -> Vec<VariableCandidate>;
pub fn derive_node_ports(analysis: &GraphAnalysis, node_id: &str) -> Vec<NodePort>;
```

AST/bundle 是编译输出模型，可直接由 Rust 调用者使用或序列化供运行时消费；本前端不提供导入未验证 bundle 的入口。JSON 文本适配器为 `compile_workflow_json`、`available_vars_json`、`node_ports_json`。它们返回 `Result<String, String>`；PyO3 / wasm-bindgen 仅转换字符串和宿主错误。

请求外壳、`analysis_json` 或 `expected_json` 无法反序列化时，是宿主参数错误。合法请求里的源语法和语言错误始终进入诊断数组。省略 `expected_json` 使用 `None`；提供该参数时必须是良构 TypeSpec 的 JSON 文本，字符串 `"null"` 不是省略。

JSON 查询参数复用有界的 JSON 值解码，再读取类型化模型，保留数字精度、映射顺序和普通对象键语义。`expected_json` 的值树深度上限为 128；`analysis_json` 为固定模型外壳另留四层余量（上限 132），覆盖成功编译生成的完整分析，包括迭代节点的注入类型。深度从根值的 0 开始计数；超限、重复键或模型不合法均返回宿主参数错误。

所有 TypeSpec 位置使用同一套类型规则。枚举中的 JSON 对象始终是对象，即使键名为 `$serde_json::private::Number` 或 `$serde_json::private::RawValue`，也不能作为数字枚举；源编译返回 `type.schema`，查询参数返回宿主错误。这些字符串仍可作为普通常量映射或分析符号表的键，合法数字枚举保持原有精度。

## 诊断和部分分析

每条诊断包含：

| 字段 | 含义 |
| --- | --- |
| `workflow_id` | 逻辑源标识 |
| `scope_graph_path` | 从根到当前内联图的宿主节点 ID 数组，根图为 `[]` |
| `target_kind` | `document`、`node`、`edge`、`port` 或 `variable` |
| `node_id`、`branch_id` | 可选节点、端口或终点分支锚点 |
| `edge_index` | 可选的原始 edges 数组索引，零起始 |
| `param_path` | 相对于原始源文档结构的 JSON Pointer，使用标准 `~0` / `~1` 转义 |
| `code`、`message` | 稳定机器代码和人类可读说明 |
| `source_location` | 解析器提供的一起始 `line` / `column`；语义诊断为 `null` |

代码族为 `parse.*`、`schema.*`、`symbol.*`、`type.*`、`builtin.*`、`topology.*`、`assignment.*`。例如 `type.projection`、`topology.missing_route`、`assignment.read`。调用者应根据 `code` 和结构锚点处理错误，不匹配 message 文案。诊断按工作流发现顺序和原始结构声明顺序排列；相同地址的检查顺序稳定。

解析和结构校验继续访问可独立检查的节点、边和工作流。图的类型、结构或拓扑前提不成立时，该图节点的 `definite_in` 为 `null`；定值断言失败同样不公布该图定值事实。被调用实现有错误时，依赖它的图也不公布事实。独立内联图可保留自己的分析。符号表与能够独立确定的端口仍可用于画布。

`GraphAnalysis` 形状如下：

```text
{
  workflow_id,
  scope_graph_path,
  symbols: Map<Identifier, TypeSpec>,
  nodes: Map<NodeId, {
    definite_in: List<Identifier> | null,
    injected: Map<Identifier, TypeSpec>,
    ports: List<NodePort> | null
  }>
}
```

`definite_in: []` 表示已证明入口集合为空；`null` 表示无法证明。请将当前编译结果的一张完整图分析交给查询接口，保持它的数字和顺序；查询没有 I/O，也不会重新编译或重新证明调用方修改过的分析。

## 补全与端口

补全返回 `[{ path: { root, fields: [...] }, typespec: TypeSpec }]`。顺序为图符号声明顺序及字段声明顺序，随后是当前节点的迭代注入项；内联迭代允许的局部遮蔽会隐藏同名外层候选。只返回已证明定值的根变量及合法的 TypedDict 投影路径。可选叶子字段被标为可空并停止向下遍历；不枚举数组索引或 OrderedMap 动态键。`expected` 通过相同的单向子类型算法过滤候选。未知节点和无可信入口事实的节点返回 `[]`。

`NodePort = { branch_id: String | null, kind: "normal" | "catch", output: [...], kill_vars: [...] }`。`branch_id: null` 是默认正常口。端口读取 Pass 3 的结果；未知节点或端口前提不成立时返回 `[]`。for_each 在外层只暴露默认正常口，输出所有汇聚目标；迭代内部正常分支和 catch 不外露。

详细 builtin 规则优先于概述：`logic` 的 `op: {value: not}` 必须绑定 `source`，禁止 `left/right`；`array_shift` 暴露 `some/empty`。`empty` 不承诺 rest，只 kill item。unwrap/get/math 的无默认 none 分支 kill 对应输出；catch 保留入口旧值，不承诺新产出。

## 归一化执行契约

`WorkflowBundle = {entry, workflows: Map<String, NormalizedRoot>}`。`NormalizedRoot = {input, output, branches, body}`；`body.kind` 为 `run` 或 `graph`。图内节点为 `{body, modifiers}`，`body.kind` 为 `run`、`graph`、`workflow` 或 `builtin`。AST 不包含解析器、文件句柄、resolver、进程状态、运行时索引位集或另一套 IR。

| 结构 | 规范化内容 |
| --- | --- |
| TypeSpec | 保留 JSON Schema 子集形状；可空 type 统一为 `[base,"null"]`，TypedDict.required 始终显式存在 |
| `BranchContract` | `{description, output: [...]}`；简写展开为全部节点输出 |
| `ArgValue` | `{kind:"path", path:{root,fields}}` 或 `{kind:"const",value:<JSON literal>}` |
| `VarDecl` | `{typespec, initial:{kind:"unbound"}}`，或 `initial:{kind:"default",value:<literal>}`，显式 null 为有效的 default 值 |
| `Bindings` | `{input, output, output_present}`；保留 output 的存在性，switch 和 compare/logic 需要区分省略与空映射 |
| `Modifiers` | `for_each`、`timeout_ms`、`retry`、`catch`；缺省值已补全，catch 通配符已展开 |
| `NormalizedGraph` | `{variables, nodes, routes, max_steps}`；routes 以 `start` / `node` / `node.branch` 索引 |
| `Route` | `{source, edge, edge_index, raw, kill_vars}`；edge 为 direct 或 fork_join，worker 顺序保持声明顺序 |

调用单次输出类型与 for_each 聚合目标类型分离保存：run/graph 的 output 和目标签名描述单次值；图符号表保存一阶提升后的集合类型。内联图只消费捕获和自动注入的输入，私有变量不会进入父图并发写集。

整数和浮点字面量按数字词法区分，布尔不属于整数。常量按目标 TypeSpec 校验，空数组、空映射和 null 不会获得 `any` 或虚构泛型。运算只做静态检查，不常量折叠、不预测运行时分支；没有足够类型上下文的复合比较常量需改用显式类型的变量。`num-bigint` 用于精确枚举数值等价和 YAML 整数字面量转换，不使用 f64 近似比较。

映射插入顺序与 worker 顺序是文本契约；类型等价独立忽略 description、属性顺序以及 required/enum 的排列。JSON 数值保持数值形状和精度，不转换成字符串；YAML 数字会规范化为合法 JSON 数字。保存原始工件文本，避免通过 JS Number 或默认浮点解析器反复编码。

源值树最多嵌套 128 层，超出返回 `parse.source`；这是一项解析资源边界，不是运行期调用深度护栏。JSON 使用标准字符串解码（包括 Unicode 代理对），拒绝解码后重名的键。YAML 支持 JSON 数据模型内的标准标签 `!!str`、`!!int`、`!!float`、`!!bool`、`!!null`、`!!seq`、`!!map`（含完整标准 URI 写法），并校验标签与值的形态。`str` 保留标量文本；`int` 接受整数写法（含十六/八进制），`float` 接受有限十进制数并保持浮点分类；`bool` 接受 true/false 的大小写变体，`null` 接受空标量、`~` 和 null 的大小写变体；`seq`/`map` 仅用于对应容器。拒绝锚点、别名、自定义标签、非 JSON 标签（如 timestamp、binary、set）及标签与值的类型不匹配。编译入口不求值 builtin、不调度执行帧、不访问 Temporal、文件系统或网络 resolver；独立运行内核和解释器见[运行时接口](runtime-api.md)。
