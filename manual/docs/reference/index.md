# 用户速查字典（User Reference）

Axcutor 工作流使用声明式的 YAML 或 JSON 文档描述数据契约与控制流图。一份工作流文档本质上是一个具备显式输入输出契约的根节点（Root Node），其执行体既可以是直接挂载 `run` 的单动作节点，也可以是由 `graph` 承载的多节点控制流图。

::: info 实现规范参引
若需查阅编译器符号表决议算法、全路径静态定值方程或运行时栈帧状态机等引擎底层实现细节，请参阅 [形式化规范（`spec/`）](../../spec/index.md)。
:::

---

## 四层结构模型

当根节点采用 `graph` 形态组织工作流时，整份文档由外向内划分为以下四个结构层级：

1. **根节点契约层（Root Node Contract）**：位于文档顶层，通过必填的 `input` 与 `output` 映射表声明整份工作流对外暴露的参数类型契约（无输入或无输出时须显式声明为空字典 `{}`），并可通过可选的 `branches` 声明根级排他控制分支及其按需交付的输出变量子集。
2. **控制流图容器层（Graph Container）**：由顶层或内联子图节点的 `graph` 字段开启，负责维护当前图级作用域（Graph Scope）。该层可配置当前栈帧的最大执行步数 `max_steps`，并通过 `variables` 显式声明图级局部变量的 `TypeSpec` 与可选的静态初值 `default`。
3. **图内多态子节点层（Inner Nodes）**：挂载于 `graph.nodes` 映射表下，每个子节点必须且只能选择以下三种多态形态之一：
   - **动作节点（`run`）**：声明 `input` / `output` 变量契约，并通过结构化 `run: { executor: ..., ... }`（如内置受信任 Python 执行器 `executor: builtin`）交由对应执行器运行。
   - **调用节点（`use`）**：调用跨文件工作流（`workflow:<path>`）或运行时内建算子（`builtin:<op>`），通过 `with.input` 与 `with.output` 显式完成跨边界的参数绑定与返回值写回。
   - **内联块级子图（`graph`）**：在当前图内嵌套独立的子图作用域，按名捕获外层变量并导出子图输出。
   此外，子节点可挂载同构集合遍历（`for_each`）、单次超时（`timeout_ms`）、原地重试（`retry`）与可恢复异常捕获（`catch`）四类执行修饰符。
4. **控制流连边层（Control Edges）**：挂载于 `graph.edges` 列表下，使用直观的箭头 DSL 串联虚拟起点 `start`、图内子节点、正常汇聚终点 `end[.branch]` 与异常中止汇点 `fail`，驱动图栈帧内的并发派发、分支路由与循环迭代。

---

## 核心语法约定

在编写工作流文档时，各层级共享以下基础语法规则：

- **标识符命名规范**：所有变量名、节点 ID、分支名及结构体字段名均须匹配正则 `^[a-zA-Z_][a-zA-Z0-9_]*$`，且不得占用控制流保留字（`start`、`end`、`fail`）或宿主代码生成保留关键字（Python `keyword.kwlist` 全部 35 个硬关键字，如 `from`、`class`、`def`、`return`、`async`、`await` 等），亦不得包含点号 `.`（点号专用于分支限定与结构体静态字段读取路径）。
- **显式静态类型声明**：所有引入新变量绑定或定义数据契约的位置（包括根节点 `input` / `output`、`graph.variables` 以及 `run` / 内联 `graph` 子节点的 `output`）均须显式标注 `TypeSpec`；编译器不做隐式类型推导，也不允许通过子节点输出隐式创建未在图级符号表中声明的变量。
- **读取与写入位置区分**：
  - **只读输入位置**：`run` 或内联 `graph` 子节点的 `input` 声明为当前图顶层变量名列表（`[<var_name>, ...]`）；在 `executor: builtin` 的 Python 代码中，通过只读模型属性 `input.<var_name>`（及结构体嵌套属性 `input.<var_name>.<field>`）读取输入快照；`use` 节点的 `with.input` 除绑定顶层变量外，还支持传入常量字面量或结构体静态字段点号路径（`<var>.<field>...`）。
  - **写入输出位置**：子节点 `output` 与 `use` 节点的 `with.output` 仅允许绑定当前图作用域的顶层可写变量名（目标变量须属于 `graph.variables` 或根/内联子图的 `output`，严禁覆写只读的 `input` 参数或做深层字段部分覆写）。

---

## 工作流文档骨架速览

以下 YAML 骨架展示了上述四层结构在标准工作流文档中的完整挂载位置：

```yaml
# ============================================================
# 1. 根节点契约层（Root Node Contract）
# ============================================================
input:
  <input_var>: <TypeSpec>               # 必填（无输入时写 {}）
output:
  <output_var>: <TypeSpec>              # 必填（无输出时写 {}）
branches:                               # 可选：根级排他分支与按需输出子集
  <branch_name>:
    description: "<desc>"
    output: [<output_var>, ...]

# ============================================================
# 2. 控制流图容器层（Graph Container）
# ============================================================
graph:
  max_steps: 1000                       # 可选：当前图栈帧最大允许步数（默认 1000）
  variables:                            # 可选：图级局部变量表（显式 TypeSpec，可选配 default）
    <init_var>:
      <TypeSpec>
      default: <literal>                # 带初值的局部变量（在 start 处已定值）
    <uninit_var>:
      <TypeSpec>                        # 未初始化局部变量（在 start 处未定值，由后续节点写入）

  # ==========================================================
  # 3. 图内多态子节点层（Inner Nodes：run / use / graph 三选一）
  # ==========================================================
  nodes:
    # 形态 A：动作节点（run）
    <action_node_id>:
      for_each: { ... }                 # 可选：同构集合遍历修饰符
      timeout_ms: 5000                  # 可选：单次尝试超时上限（毫秒）
      retry: { ... }                    # 可选：基于只读输入快照的原地重试
      catch: { ... }                    # 可选：可恢复异常捕获并物化为控制分支
      input: [<var_name>, ...]          # 可选：读取的顶层变量名列表
      output:                           # 可选：写入的顶层变量及 TypeSpec
        <out_var>: <TypeSpec>
      branches: { ... }                 # 可选：排他控制分支
      run:
        executor: builtin
        content: |
          return Branch.<choice>(<out_var>=...)

    # 形态 B：跨文件工作流或内建算子调用节点（use）
    <call_node_id>:
      use: workflow:<path>              # 或 use: builtin:<op>
      with:
        input: { <param>: <binding> }   # 显式传入变量名、结构体静态字段路径或常量字面量
        output: { <field>: <var_name> } # 显式将需要的输出字段映射回当前图顶层变量（未列出的输出自动忽略）

    # 形态 C：内联块级子图节点（graph）
    <inline_graph_node_id>:
      input: [<captured_var>, ...]      # 显式按名捕获外层图变量
      output:                           # 显式声明导出至外层图的顶层变量及 TypeSpec
        <exported_var>: <TypeSpec>
      graph:
        nodes: { ... }
        edges: [ ... ]

  # ==========================================================
  # 4. 控制流连边层（Control Edges）
  # ==========================================================
  edges:
    - start -> <call_node_id>
    - <call_node_id> -> [<worker_a>, <worker_b>] -> <action_node_id>
    - <action_node_id>.<choice> -> <inline_graph_node_id>
    - <action_node_id>.<catch_branch> -> fail
    - <inline_graph_node_id> -> end.<branch_name>
```
