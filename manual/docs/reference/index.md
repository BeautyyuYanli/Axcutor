# 用户速查字典（User Reference）

本模块汇总 Axcutor YAML 工作流文档的语法结构、各层级配置字段、6 种控制边语法、`TypeSpec` 类型写法以及 5 个运行时内建算子（`builtin:*`）的参数规范。

*(注：若需查阅编译器符号表决议算法、全路径静态定值方程或运行时栈帧状态机等引擎实现规范，请参阅 [形式化规范（`spec/`）](../../spec/index.md)。)*

---

## 工作流文档层级结构速览

一份完整的 Axcutor 工作流文档由**根节点契约**（`input` / `output` / `branches`）与**执行体**（`run` 或 `graph`）构成。以下骨架标注了各核心语法要素在文档中的挂载位置（具体字段约束与算子契约请通过左侧目录查阅）：

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
  variables:                            # 可选：图级初始状态变量（不可与宿主输入重名）
    <state_var>: <literal>
    <collection_var>:
      <TypeSpec>
      default: []

  # ==========================================================
  # 3. 图内子节点层（Inner Nodes：run / use / graph 三选一）
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
      run: "<executor_spec>"

    # 形态 B：跨文件工作流或内建算子调用节点（use）
    <call_node_id>:
      use: workflow:<path>              # 或 use: builtin:<op>
      with:
        input: { <param>: <binding> }   # 变量名、结构体静态字段路径或常量字面量
        output: { <field>: <var_name> } # 输出字段映射回当前图顶层变量

    # 形态 C：内联块级子图节点（graph）
    <inline_graph_node_id>:
      input: [<captured_var>, ...]      # 显式按名捕获外层图变量
      output: [<exported_var>, ...]     # 支持列表简写（自动复用子图符号表类型）
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
