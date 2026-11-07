# RFC 11 编译器前端实施方案

## 范围与规范依据

在尚无实现代码的仓库中建立 Rust Cargo workspace，交付 RFC 11 第三节定义的五阶段编译器、归一化 AST、画布诊断与变量补全，以及调用同一核心的 WASM 和 Python 绑定。语言行为以 `manual/spec/01-grammar-and-ast`、`02-type-system-and-symbols`、`03-static-flow-analysis` 为准，RFC 11 规定工程边界。

本次实现包含 16 个 builtin 的静态参数、类型、分支及反定值规则，以及编译常量所需的实例兼容检查；不包含 builtin 运行期求值、run 返回载荷处理、Temporal 解释器、Activity、画布 UI 或服务端存储。前后端唯一执行契约为 `NormalizedAst`，不增加字节码、低级 IR 或代码生成器。

架构依据为 `rfc/0011-compiler-and-temporal-runtime-architecture.md`。现有语言规范和使用示例保持既有语言设计；概述与算子详细规则冲突时采用详细规则：`logic(op: not)` 的操作数为 `source`，`array_shift` 的端口为 `some`、`empty`。

## 输入、输出与调用边界

核心公开以下类型化 Rust 接口：

```rust
pub fn compile_workflow(request: CompileRequest) -> CompileOutput;
pub fn get_available_vars_at_node(
    analysis: &GraphAnalysis,
    node_id: &str,
    expected: Option<&TypeSpec>,
) -> Vec<VariableCandidate>;
pub fn derive_node_ports(analysis: &GraphAnalysis, node_id: &str) -> Vec<NodePort>;
```

`CompileRequest` 包含 `entry: String` 和按工作流标识索引的 `sources`。每份 `SourceDocument` 包含 `format: "yaml" | "json"` 与 `text: String`。标识是调用方管理的非空逻辑资源名，`use: workflow:<id>` 按完整后缀精确匹配；核心不解释磁盘路径、不读取文件、不访问网络。调用方提供源文档快照，编译结果不保留解析器、宿主回调或进程全局状态。

```json
{
  "entry": "main",
  "sources": {
    "main": {
      "format": "yaml",
      "text": "input: {}\noutput: {}\nrun: do work\n"
    }
  }
}
```

`CompileOutput` 包含：

| 字段 | 契约 |
| --- | --- |
| `diagnostics` | 按工作流发现次序、图与源码声明顺序稳定输出的诊断数组。 |
| `analysis` | 每个可分析图的符号表、节点入口定值集、迭代注入类型与派生端口；不包含无法证明的定值事实。 |
| `bundle` | 无错误时为 `{ entry, workflows }`，`workflows` 只包含入口及传递引用工作流的归一化 AST；有任何入口闭包错误时为 `null`。 |

仅编译入口的传递引用闭包，未被引用的 `sources` 不影响该入口。先提取闭包内全部顶层签名，再检查各工作流实现；用已访问集合终止依赖发现，允许直接和间接递归，不展开递归图体。缺失或签名非法的目标产生调用位置诊断，不能作为空签名继续生成执行工件。

WASM 与 Python 均导出 `compile_workflow(request_json) -> result_json`、`get_available_vars_at_node(analysis_json, node_id, expected_json?) -> candidates_json`、`derive_node_ports(analysis_json, node_id) -> ports_json`。JSON 外壳无法解析时返回宿主参数错误；合法请求中的源文档和语言错误均进入 `CompileOutput.diagnostics`。两个绑定只负责文本和错误转换，不重写静态分析。Python 模块名为 `axcutor_core`，通过 maturin 构建。

文本边界避免先经 JS 对象传递源文档而丢失数字词法和映射插入顺序。`serde_json` 启用 `preserve_order` 与 `arbitrary_precision`；整数与浮点常量按数字原始表示区分，布尔值不视为整数。归一化工件中的 JSON 数值仍保持数值表示，不隐式改成字符串；消费者应保存原始工件文本，不经 JS 数值转换后重新发布。

## 数据模型与所有权

源文档只在一次编译调用内存在。解析得到的 JSON 值是输入容器，不作为可执行 AST 暴露；通过检查后构造一套类型化 `NormalizedAst`。Rust 所有权保证工件自包含，跨语言边界返回拥有独立存储的 JSON 文本。没有缓存失效、数据库、文件监视器或持久化分析会话。

| 模型 | 内容与不变式 |
| --- | --- |
| `TypeSpec` | 基础类型、可空性、description，以及 Scalar、Array、TypedDict、OrderedMap 四种互斥构造器。TypedDict 的 `required` 始终显式存在。 |
| `FieldPath` / `ArgValue` | 路径保存根变量与字段列表；绑定仅有路径引用和 JSON 常量两态。保留 `default` 缺省与显式 `null` 的区别。 |
| `NormalizedRoot` / `NormalizedNode` | 根为 run 或 graph；图内为 run、graph、workflow/builtin call。保留模板、显式绑定、单次输出类型及修饰符。 |
| `BranchContract` | 描述与显式输出子集；简写展开为该节点全部输出字段。外部工作流分支输出经 `with.output` 映射至本图变量。 |
| `NormalizedGraph` | 变量声明、节点、按源端点字符串索引的路由表。源码边索引和 raw 文本用于定位，不产生另一套控制流表示。 |
| `EdgeAst` | Direct 或 ForkJoin；并发成员保持声明顺序，join 不允许 fail。每条路由附有该源分支的 `kill_vars`。 |
| `NodePort` | 默认正常口或具名正常/异常口及其输出承诺和 kill 集。挂载 `for_each` 后对外只暴露默认正常口。 |
| `GraphAnalysis` | 工作流标识、从根到内联图的节点路径、显式符号表与节点分析。编译内部位集索引不成为运行时变量标识。 |

类型等价忽略 description、属性声明顺序、required 和 enum 的排列；类型兼容遵循单向子类型规则，不通过简单 JSON 对象相等代替。保留映射的声明顺序用于常量及确定性结果，涉及集合的语义比较独立于该顺序。

## 五阶段编译流水线

### Pass 1：解析与消糖

YAML 使用维护中的 Rust 解析库 `yaml-rust2`，通过事件接口转换为 JSON 兼容值，保留数值文本、映射顺序和定位信息；不自行实现 YAML 语法。JSON 先按 JSON 文法检查，再共用文档转换与语言检查。拒绝重复映射键、非字符串映射键、多文档源、非有限数字和不属于 JSON 数据模型的 YAML 扩展。别名和自定义标签不进入语言 AST，产生解析诊断。

按显式字段白名单检查根、图、三类节点和修饰符，未知字段不得静默忽略。按 ASCII 标识符及保留字规则区分变量、节点、业务分支、异常分支；校验输入去重、输出绑定单射、BranchSpec 子集、retry/catch 选择器互斥、正整数约束和 `for_each` 有效输出非空。

用完整匹配的边文法产生 Direct/ForkJoin AST，只允许规范定义的水平空白，禁止点号两侧空白、换行、三段普通链及非法虚拟端点。提取 `run` 的最长 `variables.<path>` 引用，句尾标点不成为路径组成部分。展开分支简写与 catch 通配符，保留修饰符的默认值。

语法错误只阻断依赖该结构的后续检查；其他节点、其他边和其他工作流继续收集错误。不能用空节点、空类型或默认路由替代非法结构并据此宣称编译成功。

### Pass 2：类型良构、作用域与显式符号表

递归校验 TypeSpec 字段闭合性、可空联合、标量枚举、列表元素、结构体字段和 required、有序映射值类型，并补全省略的 required。实例检查处理默认常量与显式 null，闭合结构体拒绝额外字段。

每张图先收集宿主输入、宿主输出、graph.variables、未挂载 `for_each` 的 run/graph 输出四类显式声明。同名声明必须结构化等价；builtin、workflow call 和 `for_each` 不自动声明输出变量。宿主输入与 graph.variables 严格互斥。源集合类型确定后生成 item/key/index 注入表，并依节点类型检查外层或子图边界冲突。

内联图只继承 input 捕获项及自动注入项，不隐式访问父图其他变量；单次图输出始终使用未提升的元素类型。图与调用的所有权边界由显式输入和输出映射确定。

### Pass 3：节点类型、builtin 与分支效果

`TypeOf` 只允许穿透非空 TypedDict；中间字段须 required，末级可选字段生成可空类型；禁止对 array、OrderedMap、标量或可空前缀执行点投影。run 中的引用必须属于其显式 input 白名单。

实现整数向 number 拓宽、可空提升、枚举包含、Array/OrderedMap 协变及闭合 TypedDict 的属性域和 required 规则。引用使用子类型检查，字面量直接按目标类型检查，不对空容器、null 或异构常量发明通用类型。

workflow call 的 input 键必须完整匹配目标签名；output 允许只接收目标字段的子集，未映射字段忽略。检查目标声明、映射单射、分支输出映射及 catch 与被调方业务分支的碰撞。递归调用只消费预收集签名。

16 个 builtin 按规范第五章各自确定参数键、输出键、字面量 op、泛型约束、默认值、异名限制和端口，按语义分为以下四组实现，复用 TypeOf、子类型和实例检查：

| 组 | 算子 |
| --- | --- |
| 标量 | assign、unwrap |
| 控制与数值 | switch、compare、logic、math、length |
| 列表 | array_get、array_shift、array_append、array_concat、array_flatten |
| 映射与结构体 | map_get、map_set、map_delete、object_merge |

unwrap/get/math 的 none 路由反定值对应输出；array_shift.empty 仅反定值 item，不定值 rest；catch 不承诺新输出且不抹除父图旧值。for_each 对 source 类型执行一阶 Lift，检查提升后目标已显式声明且兼容；所有内部业务和 catch 分支由迭代消费，对外输出汇聚结果。

### Pass 4：拓扑与并发写入

按派生端口验证 start 唯一、正常和异常出边穷尽且唯一、目标存在以及宿主 end 分支匹配。重复源端点先报错，不能由路由表覆盖而丢失证据。

ForkJoin 成员仅属于一个并发边，不能出现在本边首尾或其他边中；未挂载 for_each 的成员不得暴露正常或异常分支。写冲突只比较提交至父图的输出集合，子图私有变量不计入。

将并发边按源、各成员和 join 建立临时遍历视图，前向遍历验证无死代码，反向终态遍历验证每个节点可退至 end/fail，并要求 start 至少可达一个正常 end。反向可达性已覆盖无出口 SCC，不额外保存或执行重复的 Tarjan 检查。临时遍历视图随编译结束销毁，不进入执行工件。

### Pass 5：分支敏感必达定值

为显式符号分配一次性位集索引，start 初始化为 HostIn 与具有 default 键的变量；其他可分析入口初始化为全集。按最大不动点迭代：普通汇合取入边交集，业务分支先 kill 后加入承诺输出，catch 保留原入口事实。只在收敛后检查读取与正常终点输出。

ForkJoin 每个成员从相同的源分支输出定值集读取；join 在该源集合上并入全部成员输出。禁止把成员按顺序串联分析，也不能让兄弟输出出现在另一成员入口。

非法结构、未决类型或拓扑不满足前提的图不输出看似可信的全量定值结论；仍保留可确定的符号与端口，并继续处理其他独立图。内联图使用自身宿主输入初始化单独求解，父图只消费其边界契约。

## 诊断与画布分析

诊断包含 `workflow_id`、`scope_graph_path`、`target_kind`、可选 `node_id`、`branch_id`、`edge_index`、`param_path`，以及稳定的 `code` 和可读的 `message`。作用域路径为内联宿主节点标识数组；param_path 使用 JSON Pointer。解析错误附解析器能证明的源码位置，不能用字符串搜索伪造 span；语义错误以结构地址保证画布精确定位。

错误代码按 parse、schema、symbol、type、builtin、topology、assignment 分类，不以 message 文案作为机器协议。结果按声明顺序稳定生成；同一根因不通过虚构事实扩散成后续错误。

节点补全由已证明的入口定值变量和该节点可见的迭代注入项组成。递归列举合法 TypedDict 字段路径：末级可选字段可列出并标为可空，但不再向其内部穿透；不列出 map 动态键和数组索引。expected 存在时用同一子类型算法过滤。无法证明入口定值时不返回变量候选。

端口查询只读取分析结果中由 Pass 3 推导的正常和 catch 口，不另写一套分支规则。补全和端口查询均无 I/O、无状态，与编译调用使用相同的 GraphAnalysis JSON 契约。

## 工程组织

```text
Cargo.toml
Cargo.lock
crates/
  axcutor-core/
    src/ast/                 # 唯一归一化 AST、TypeSpec、绑定与边
    src/compiler/            # 解析、五阶段、诊断、画布查询
    tests/                   # 通过公开接口保护语言与工件契约
  axcutor-wasm/
    src/lib.rs               # wasm-bindgen 文本薄绑定
  axcutor-py/
    pyproject.toml            # maturin 构建配置
    src/lib.rs               # axcutor_core Python 文本薄绑定
README.md                    # 实现边界与公开接口说明
.github/workflows/compiler.yml
```

核心依赖采用 serde、serde_json、indexmap、bitvec、regex、yaml-rust2；绑定分别依赖 wasm-bindgen 和 PyO3。只添加实际使用的依赖，不引入插件注册框架、增量编译框架或自定义通用图引擎。Cargo.lock、WASM glue、wheel 等生成物由对应工具生成，源码和配置通过结构化 patch 修改。
