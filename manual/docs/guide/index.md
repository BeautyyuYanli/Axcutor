# 渐进式教程（Guide）

实际运行入口见 [使用 Docker 执行工作流](./00-run-with-docker.md)，包含基础设施启动、CLI 文件提交、HTTP 调用与 Temporal Web UI 查询方式。接口字段与状态模型见 [Execution HTTP API](../reference/04-execution-http-api.md)。

浏览器内编辑、静态分析与执行调试见 [Web Playground](./00-web-playground.md)。

Axcutor 是一套将**显式控制流图（Control-Flow Graph）**与**强类型数据流契约（Data-Flow Contract）**相结合的声明式工作流编排语言。在编写工作流时，开发者无需关注底层异步状态机与并发锁的维护，只需围绕**控制流拓扑**与**栈帧变量表**构建清晰的计算模型。

---

## 核心执行模型：控制流与数据流解耦

Axcutor 将工作流的运行机制拆分为正交的两个层面——**何时执行（控制流）**与**读写什么数据（数据流）**：

### 1. 控制令牌（Control Token）驱动图拓扑流转

工作流的控制流由有向图中的 `edges` 显式定义，运行时通过**控制令牌（Control Token）**在节点间的传递来推进状态：

- **单令牌顺序与分支路由**：工作流启动时，虚拟入口 `start` 产生一枚控制令牌并沿连边向下游推进。遇到声明了排他分支（`branches`）或异常捕获（`catch`）的节点时，节点在执行完毕后必须且只能激活其中一个分支端口，令牌随之流向唯一下游路径；当有向边回指上游节点时，令牌即在图内形成动态收敛循环（`while`）。
- **结构化并发分裂与汇聚**：当令牌流经异构并发组（`-> [a, b] ->`）或同构集合遍历修饰符（`for_each`）时，主令牌会在分叉点分裂为多枚子令牌并发执行，并在隐式同步屏障（Join Barrier）处等待所有分支完成后重新合并为单枚主令牌继续向下游推进。
- **确定性终结**：令牌抵达虚拟终结点 `end`（或分支终结点 `end.<branch>`）时当前图正常收敛并提取输出；若流向 `fail` 终结点或触发未捕获的运行期异常，当前图立即中止并向上层调用栈传播失败状态。

### 2. 图栈帧变量表（Variables）承载不可变数据流

节点之间不通过隐式通道或消息队列直接耦合，所有跨节点的数据交换均以**图栈帧变量表（`variables`）**为唯一媒介：

- **节点黑盒与显式读写契约**：每个节点都是独立的黑盒计算单元。节点必须通过 `input` 显式声明所需读取的变量子集，通过 `output` 声明写回变量表的字段及其 JSON Schema 类型（`TypeSpec`）；未在契约中声明的变量对节点完全不可见。
- **不可变值语义与原子提交**：节点读取输入时获取的是变量的不可变快照；节点执行成功后，其 `output` 产出以原子方式合并回当前图栈帧的变量表。在并发分叉区间内，各并发分支的写集严格互斥，或由 `for_each` 隔离在独立的迭代子作用域并在汇聚时按序归集为列表或有序映射。
- **作用域隔离**：跨包调用（`use: workflow:<pkg_path>`）会创建完全隔离的函数栈帧（`Function Frame`），仅通过参数绑定（`with`）交换数据；图内嵌子图（`graph`）则创建块级栈帧（`Block Frame`），支持显式捕获外层变量与子图级失败写集回滚。

---

## 编译期静态验证与运行期调度生命周期

一份 Axcutor 工作流从声明到执行完毕，经历**编译期静态分析**与**运行期确定性调度**两个阶段：

1. **编译期静态检查（Compile-Time Verification）**
   在工作流投入运行前，编译器会对 AST 与控制流图执行完备的静态验证：
   - **拓扑可达性与结构化检查**：校验所有节点均处于从 `start` 到 `end`/`fail` 的有效路径上，确保所有分支端口全覆盖、并发分叉严格闭合为单入口单出口（SESE）区域，且不存在死锁或孤立节点。
   - **单遍显式符号表构建与全路径定值分析（Definite Assignment）**：汇总当前图的 `input`、`output`、`graph.variables` 及内部节点的 `output` 显式类型声明构建静态符号表，并沿所有可能的控制流路径进行数据流分析，确保任何节点的输入变量以及图出口的 `output` 在被读取前**全路径必达已赋值**，在编译期彻底消除未初始化读取与并发写写冲突（Write-Write Race）。

2. **运行期执行与护栏调度（Runtime Execution & Guardrails）**
   通过静态验证后，工作流引擎依据外部传入的初始参数初始化根栈帧变量表，并注入初始控制令牌：
   - **节点调度与执行契约校验**：令牌激活节点后，引擎从变量表提取 `input` 快照，交由底层执行器（如内置受信任内联 Python 执行器 `executor: builtin`、包内独立模块级 Python 执行器 `executor: module`、外部扩展执行器或 `builtin:*` 内建确定性算子）运行，并在节点返回时严格按照其 `output` Schema 校验产出数据。
   - **多级护栏与事务级容错**：运行期全程受声明式护栏保护——图层级通过 `max_steps` 限制最大推进步数以防范无限循环；节点层级通过 `timeout_ms` 与 `retry` 实现基于输入快照的幂等超时重试，并通过 `catch` 将超时或执行期异常转化为控制流恢复分支降级处理。

---

## 工作流结构概览

以下完整示例展示了根节点契约、图内节点声明、并发与容错修饰符以及控制流连边如何协同工作：

```yaml
# 根节点契约：定义工作流对外暴露的入参与最终出参 Schema
input:
  raw_items:
    type: array
    items: { type: string }
output:
  processed_items:
    type: array
    items: { type: string }
  audit_report:
    type: string

graph:
  # 图级执行护栏：限制当前图最大推进步数
  max_steps: 100

  nodes:
    # 节点 1：同构集合并发（for_each）+ 超时重试（timeout_ms / retry）+ 异常捕获（catch）
    batch_process:
      for_each:
        source: raw_items
        item: single_item
        concurrency: 4
      timeout_ms: 3000
      retry:
        max_attempts: 2
        on: [execution, timeout]
      catch:
        skipped: "*"
      input: [single_item]
      # 声明单次迭代的元素类型 string；汇聚后在外层变量表中自动提升为 Array<string>
      output:
        processed_items: { type: string }
      run:
        executor: builtin
        content: |
          return Output(processed_items=input.single_item.strip().upper())

    # 节点 2：跨包调用子工作流（use + with 显式参数绑定）
    generate_audit:
      use: workflow:compliance.build_audit_report
      with:
        input:
          items: processed_items
          strict: { value: true }
        output:
          report: audit_report

  # 控制流连边：定义控制令牌从入口、业务节点到终结点的流转拓扑
  edges:
    - start -> batch_process
    - batch_process -> generate_audit
    - generate_audit -> end
```
