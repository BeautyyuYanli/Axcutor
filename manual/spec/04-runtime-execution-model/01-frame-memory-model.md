# 01. 栈帧生命周期与内存模型（Frame Memory Model）

运行时引擎维护一个树状激活栈帧结构（支持并发分支沿树分叉）。每个栈帧持有独立的变量绑定表 $\rho : \text{Identifier} \rightharpoonup \text{Value}$。

---

## 一、 三类栈帧的构造与销毁生命周期

### 1. 函数调用栈帧（`FunctionFrame`）
* **创建时机**：根工作流启动，或图内执行 `CallNode`（`use: workflow:<path>`）。
* **初始内存绑定 $\rho_0$**：
  * 仅由调用方传入的 `input` 参数深拷贝（或不可变值引用）与目标图自身的 `graph.variables` 初始值构成：
    $$\rho_0 = \rho_{\text{caller\_in}} \uplus \rho_{\text{graph\_defaults}}$$
  * 与父调用栈帧的内存表完全物理隔离（无词法作用域穿透）。
* **正常提交（`end` / `end.<branch>`）**：
  * 提取所选出口分支承诺产出的 `output` 子集字段，经 `TypeSpec` 运行期验证（及 `with.output` 键名重映射）后，原子合并至父栈帧；随后销毁当前 `FunctionFrame`。
* **异常回滚（`fail` 或未捕获异常）**：
  * 立即销毁当前 `FunctionFrame`，丢弃 $\rho$ 中一切状态，向父节点边界抛出对应异常。

### 2. 块级作用域栈帧（`BlockFrame`）
* **创建时机**：图内执行 `InlineGraphNode`（内联 `graph`，或挂载了 `for_each` 的内联 `graph` 的每次元素迭代）。
* **初始内存绑定 $\rho_0$**：
  * 从外层父图当前状态快照 $\rho_{\text{parent\_snap}}$ 中仅提取 `node.input` 列表中显式声明的变量，加上 `for_each` 注入项 $\rho_{\text{iter}}$（若有）、`for_each.state` 当前值 $\rho_{\text{state}}$（若有）以及子图自身的 `subgraph.variables` 初始值：
    $$\rho_0 = \rho_{\text{parent\_snap}}|_{N.\text{input}} \uplus \rho_{\text{iter}} \uplus \rho_{\text{state}} \uplus \rho_{\text{subgraph\_defaults}}$$
* **提交与回滚**：
  * 子图内部节点的写入仅更新该 `BlockFrame` 自身的 $\rho$；
  * 到达子图 `end`（或 `end.<branch>`）时，提取关联的 `output` 子集提交给外层节点（或 `for_each` 收集器）；
  * 到达子图 `fail` 或发生未捕获异常时，直接丢弃整个 `BlockFrame`，外层父图的 $\rho_{\text{parent}}$ 保持零污染。

### 3. 迭代局部隔离帧（`IterationFrame`）
* **创建时机**：在挂载了 `for_each` 的 `ActionNode`（`run`）或 `CallNode`（`use`）中，针对源集合的每一个元素创建。
* **初始只读视图**：在外层图锁定的只读快照 $\rho_{\text{parent\_snap}}$ 与当前 `state` 值之上叠加当次元素的 `{ item, key?, index? }`。
* **产出隔离**：单次迭代返回的非 `state` 字段仅暂存在 `for_each` 协调器的槽位缓冲中，绝不直接写入外层图的 $\rho_{\text{parent}}$。

---

## 二、 节点激活的最小权限只读快照与原子写回

1. **只读输入提取（Read-Only Snapshot Extraction）**：
   * 任意节点激活时，运行时仅按其声明的读取集合提取变量快照（不可变视图或深拷贝）传入执行器；执行器在运行期间对传入对象的任何原地修改绝不影响栈帧中的 $\rho$。
2. **原子校验写回（Atomic Validated Commit）**：
   * 节点正常返回 `(branch, raw_output)` 后，运行时首先验证 `branch` 合法，且 `raw_output` 中包含该 `branch` 契约要求的全部字段并逐一通过 `TypeSpec` Schema 校验（若违约则抛出 `contract` 异常并丢弃 `raw_output`）；
   * 校验全部通过后，仅将该分支契约允许的字段子集一次性原子更新至所属栈帧的 $\rho$。
