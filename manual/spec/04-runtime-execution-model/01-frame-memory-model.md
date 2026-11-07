# 01. 栈帧生命周期与内存模型（Frame Memory Model）

运行时引擎维护一个树状激活栈帧结构（支持并发分支沿树分叉）。每个栈帧持有独立的变量环境（变量绑定表）：

$$\rho : \text{dom}(\Sigma_G) \to \text{JSONValue} \cup \{\text{Unbound}\}$$

其中：
* **值域与绑定状态区分**：
  * $\rho(v) \in \text{JSONValue}$ 表示变量 $v$ 处于**已绑定有效 JSON 值**状态（包含合法的 JSON `null` 值，前提是该变量的 `TypeSpec` 允许 `null`）；
  * $\rho(v) = \text{Unbound}$ 表示变量 $v$ 处于**未绑定状态（`Unbound`）**（即变量虽在当前图符号表 $\Sigma_G$ 中声明，但尚未被初始值或任何前驱节点写入）。
  * 等价偏函数表示：记已绑定有效值的变量子集为有效定义域 $\text{dom}_{\text{bound}}(\rho) = \{v \in \text{dom}(\Sigma_G) \mid \rho(v) \neq \text{Unbound}\}$；下文对局部映射片段（如 $\rho_{\text{caller\_in}}$、$\Delta$ 等）省略未绑定项，简记其有效绑定域为 $\text{dom}(\cdot)$。
* **环境映射与更新算子**：
  * **子集投影算子 $\rho|_V$**：将环境 $\rho$ 限制在变量子集 $V \subseteq \text{dom}(\Sigma_G)$ 上，满足 $(\rho|_V)(v) = \rho(v)$（当 $v \in V$）且 $(\rho|_V)(v) = \text{Unbound}$（当 $v \notin V$）。
  * **无交集并算子 $\rho_1 \uplus \rho_2$**：仅当两个映射片段的已绑定变量域互不相交（即 $\text{dom}(\rho_1) \cap \text{dom}(\rho_2) = \emptyset$）时有定义，合并结果满足：
    $$(\rho_1 \uplus \rho_2)(v) = \begin{cases} \rho_1(v), & v \in \text{dom}(\rho_1) \\ \rho_2(v), & v \in \text{dom}(\rho_2) \\ \text{Unbound}, & \text{otherwise} \end{cases}$$
  * **右优先覆盖合并算子 $\rho \triangleleft \Delta$**：用新产出的有效绑定增量映射 $\Delta : \text{dom}(\Delta) \to \text{JSONValue}$（其中 $\text{dom}(\Delta) \subseteq \text{dom}(\Sigma_G)$）原子覆盖更新原环境 $\rho$，满足：
    $$(\rho \triangleleft \Delta)(v) = \begin{cases} \Delta(v), & v \in \text{dom}(\Delta) \\ \rho(v), & v \notin \text{dom}(\Delta) \end{cases}$$

---

## 一、 三类栈帧的构造与销毁生命周期

### 1. 函数调用栈帧（`FunctionFrame`）
* **创建时机**：根工作流启动，或图内执行 `CallNode`（`use: workflow:<path>`）。
* **初始内存绑定 $\rho_0$**：
  * 仅由调用方传入的 `input` 参数深拷贝（或不可变值引用）与目标图自身的 `graph.variables` 中声明了 `default` 键的变量子集 $\text{InitVars}(G)$ 的初始值深拷贝 $\rho_{\text{graph\_defaults}}$ 构成（在 `graph.variables` 中未声明 `default` 键的局部变量在栈帧创建时保持未绑定 `Unbound`）：
    $$\rho_0 = \rho_{\text{caller\_in}} \uplus \rho_{\text{graph\_defaults}}$$
  * 与父调用栈帧的内存表物理隔离（无词法作用域穿透）。
* **正常提交（`end` / `end.<branch>`）**：
  * 提取所选出口分支承诺产出的 `output` 子集字段，经 `TypeSpec` 运行期校验后：仅提取 `with.output` 中显式映射的目标字段（未在 `with.output` 中列出的其余输出字段自动丢弃）；若未挂载 `for_each`，则直接写回父栈帧；若挂载了 `for_each`，则按 `with.output` 重映射为外层目标变量名后提交给 `for_each` 槽位缓冲；随后销毁当前 `FunctionFrame`。
* **异常回滚（`fail` 或未捕获异常）**：
  * 立即销毁当前 `FunctionFrame`，丢弃 $\rho$ 中的全部状态，向父节点边界抛出对应异常。

### 2. 块级作用域栈帧（`BlockFrame`）
* **创建时机**：图内执行 `InlineGraphNode`（内联 `graph`，或挂载了 `for_each` 的内联 `graph` 的每次元素迭代）。
* **初始内存绑定 $\rho_0$**：
  * 从外层父图当前状态快照 $\rho_{\text{parent\_snap}}$ 中仅提取 `node.input` 列表中显式声明的变量，加上 `for_each` 注入项 $\rho_{\text{iter}}$（若有）以及子图自身的 `subgraph.variables` 中声明了 `default` 键的变量子集 $\text{InitVars}(G_{\text{sub}})$ 的初始值深拷贝 $\rho_{\text{subgraph\_defaults}}$（未声明 `default` 键的局部变量在栈帧创建时保持未绑定 `Unbound`）：
    $$\rho_0 = \rho_{\text{parent\_snap}}|_{N.\text{input}} \uplus \rho_{\text{iter}} \uplus \rho_{\text{subgraph\_defaults}}$$
* **提交与回滚**：
  * 子图内部节点的写入仅更新该 `BlockFrame` 自身的 $\rho$；
  * 到达子图 `end`（或 `end.<branch>`）时，提取关联的 `output` 子集提交给外层节点（或 `for_each` 槽位缓冲）；
  * 到达子图 `fail` 或发生未捕获异常时，直接丢弃整个 `BlockFrame`，外层父图的 $\rho_{\text{parent}}$ 保持不变。

### 3. 迭代局部隔离帧（`IterationFrame`）
* **创建时机**：在挂载了 `for_each` 的 `ActionNode`（`run`）或 `CallNode`（`use`）中，针对源集合的每一个元素创建。
* **初始只读视图**：在外层图锁定的只读快照 $\rho_{\text{parent\_snap}}$ 之上叠加当次元素的 `{ item, key?, index? }`。
* **产出隔离**：单次迭代返回的字段仅暂存在 `for_each` 协调器的槽位缓冲中，待全部元素迭代完成后按序汇聚并一次性写入外层图的 $\rho_{\text{parent}}$。

---

## 二、 节点激活的最小权限只读快照与原子写回

1. **只读输入提取（Read-Only Snapshot Extraction）**：
   * 任意节点激活时，运行时仅按其声明的读取集合提取变量快照（不可变视图或深拷贝）传入执行器；执行器在运行期间对传入对象的原地修改不影响栈帧中的 $\rho$。
2. **原子校验写回（Atomic Validated Commit）**：
   * 节点正常返回 `(branch, raw_output)` 后，运行时首先验证 `branch` 合法，且 `raw_output` 中包含该 `branch` 契约要求的全部字段并逐一通过 `TypeSpec` Schema 校验（若违约则抛出 `contract` 异常并丢弃 `raw_output`）；
   * 校验全部通过后，仅将该分支契约允许的字段子集 $\Delta_{\text{valid}}$ 一次性通过右优先覆盖算子 $\rho \leftarrow \rho \triangleleft \Delta_{\text{valid}}$ 原子更新至所属栈帧的 $\rho$（或写入 `for_each` 的当次迭代槽位缓冲）。
