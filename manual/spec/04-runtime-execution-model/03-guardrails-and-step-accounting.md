# 03. 运行护栏计量规则与内建算子运行期语义（Guardrails, Step Accounting & Builtin Semantics）

本规范定义单图步数计数器（`steps`）与超限并发协同取消协议、调用栈深度计数器（`call_depth`）、节点单次尝试超时计时器（`timeout_ms`），以及内建泛型算子（`builtin:*`）的运行期求值与计步语义。

---

## 一、 单图步数计数器与超限协同取消（`max_steps` Accounting & Cooperative Cancellation）

每个激活的图栈帧 $F_G$（含根图、`use` 调用的子工作流图、每次激活的内联 `graph` 子图）维护一个局部变量 $\text{steps}(F_G)$，初始值为 $0$，上限为 $G.\text{max\_steps}$（未显式声明时默认值为 $1000$）。

1. **增量计步公式**：
   * **激活未挂载 `for_each` 的单个节点 $N$（含 `run`、内联 `graph`、`use: workflow:<path>` 以及 `use: builtin:<op>` 内建算子节点）**：
     在启动 $N$ 之前，执行 $\text{steps}(F_G) \leftarrow \text{steps}(F_G) + 1$。
     *(注：每次激活 `use: builtin:<op>` 节点同样计 $1$ 步，确保由纯 `builtin:*` 节点构成的循环同样受 `max_steps` 护栏保护；节点 $N$ 内部的 `retry` 重试不额外增加所属图 $F_G$ 的 $\text{steps}$；若 $N$ 是内联 `graph` 或 `use` 节点，$N$ 在父图 $F_G$ 中仅计 $1$ 步，而 $N$ 内部展开的子图栈帧 $F_{\text{sub}}$ 独立从 $0$ 开始计量其自身的 $\text{steps}(F_{\text{sub}})$。)*
   * **激活 Fork-Join 并发组 `[W_1, ..., W_k]`**：
     对组内每个未挂载 `for_each` 的成员节点计 $1$ 步（挂载了 `for_each` 的成员节点按其元素数计步）。
   * **激活挂载了 `for_each` 的节点 $M$（源集合长度为 $n = |S|$）**：
     每启动第 $i$ 个元素（$0 \le i < n$）的迭代前，执行 $\text{steps}(F_G) \leftarrow \text{steps}(F_G) + 1$（共累加 $n$ 步；当 $n = 0$ 时累加 $0$ 步）。
2. **超限触发判定与在途并发协同取消协议**：
   * 每当图栈帧 $F_G$（包括并发 `for_each` 准备激活下一个新元素迭代，或并发组分支推进时）检测到 $\text{steps}(F_G) > G.\text{max\_steps}$ 时：
     1. **停止派发与广播取消**：立即拒绝激活新节点或新元素迭代，并向当前图栈帧 $F_G$ 树下所有尚未完成的在途并发任务（包括 Fork-Join 并发组成员、`for_each` 在途并发迭代及其派生的子栈帧）广播不可捕获的 **`CancelledError`**（触发底层执行器 `abort_signal`）；
     2. **等待收敛与销毁栈帧**：等待 $F_G$ 下全部在途并发任务退出后，销毁并回滚图栈帧 $F_G$（丢弃一切未提交的局部状态与缓冲槽位）；
     3. **向上抛出异常**：
        * 若 $F_G$ 为子图栈帧（内联 `graph` 或 `use` 子工作流），向父节点边界抛出可恢复异常 **`max_steps`**；
        * 若 $F_G$ 为最顶层根图栈帧，立即抛出不可恢复的引擎级 **`MaxStepsExceededError`** 终止整个工作流。

---

## 二、 调用树深度追踪（`max_call_depth` Accounting）

运行时为每一条执行路径维护调用栈深度 $d$（上限为引擎配置参数 $\text{MAX\_CALL\_DEPTH}$，默认 $64$）：

* 根工作流启动时：$d = 1$；
* 沿当前执行路径每进入一层 `FunctionFrame`（`use: workflow:<path>`）或 `BlockFrame`（内联 `graph`），子栈帧的深度为：
  $$d_{\text{child}} = d_{\text{parent}} + 1$$
* 当 $d_{\text{child}} > \text{MAX\_CALL\_DEPTH}$ 时，立即抛出不可捕获的引擎级致命终止 **`StackOverflowError`**。
* **并发分叉独立性**：当在深度为 $d$ 的父帧中分叉启动并发组 `[W_1, ..., W_k]` 或并发 `for_each` 的多个迭代时，每个并发子分支均以 $d$ 为基数各自独立计算 $d + 1$，兄弟分支之间不横向累加。

---

## 三、 单次尝试计时器（Per-Attempt `timeout_ms` Timer）

当节点 $N$ 声明了 `timeout_ms: T`（$T > 0$）时：

* 计时器绑定在节点 $N$（或 `for_each` 下的单次元素迭代）的**每一次独立尝试（Attempt $a \in \{1, \dots, \text{max\_attempts}\}$）**上；
* 在第 $a$ 次尝试启动瞬间开启单调时钟计时器（预算为 $T$ 毫秒）；若在 $T$ 毫秒内该次尝试未完成，立即取消该次尝试（若为子图则级联取消子图内部所有在途任务），丢弃该次尝试的局部状态，并产生 **`timeout`** 异常交由该节点的 `retry` / `catch` 状态机处理。

---

## 四、 内建算子（`builtin:*`）运行期求值语义

所有 `use: builtin:<op>` 节点均为常数时间（或与浅拷贝集合/结构体长度成正比）、无副作用、不抛出可恢复运行时异常的确定性内建转换。每次激活同样遵循第一节的计步规则（$\text{steps}(F_G) \leftarrow \text{steps}(F_G) + 1$），并按以下规则更新当前栈帧变量绑定表 $\rho$：

1. **纯函数式不可变值语义（Copy-on-Write Immutability）**：
   * `builtin:array_shift`、`builtin:array_append`、`builtin:map_set`、`builtin:map_delete` 与 `builtin:object_merge` 均采用纯函数式不可变更新：始终返回新构造的集合或结构体副本（或基于结构共享的持久化不可变数据结构），绝不原地修改当前栈帧快照或历史快照引用中的旧集合或结构体对象。
2. **各算子运行期求值与反定值（Unbind）状态转移**：
   * **`builtin:unwrap`**：
     * 读取源值 $x = \text{Eval}(\rho, \text{with.input.source})$；
     * **未声明 `default` 时**：
       * 若 $x \neq \texttt{null}$，绑定 $\rho(v_{\text{out}}) \leftarrow x$，控制令牌转入 `.some` 出边；
       * 若 $x = \texttt{null}$，将当前栈帧中的目标变量显式置为未绑定（$\rho(v_{\text{out}}) \leftarrow \text{Unbound}$，消除任何前置旧值），控制令牌转入 `.none` 出边。
     * **声明了 `default` 时**：
       * 若 $x \neq \texttt{null}$ 则令 $y = x$，否则令 $y = \text{Eval}(\rho, \text{with.input.default})$；绑定 $\rho(v_{\text{out}}) \leftarrow y$，控制令牌沿唯一无分支出边前进。
   * **`builtin:switch`**：
     * 读取源值 $x = \text{Eval}(\rho, \text{with.input.source})$；不修改 $\rho$，若 $x$ 为布尔值则转入 `.true` 或 `.false` 出边，若 $x$ 为枚举字符串则转入与 $x$ 字面量同名的 `.<x>` 出边。
   * **`builtin:assign`**：
     * 读取源值 $x = \text{Eval}(\rho, \text{with.input.source})$（支持顶层变量、`FieldPathExpr` 静态字段路径投影或常量字面量）；
     * 绑定 $\rho(v_{\text{out}}) \leftarrow x$，控制令牌沿唯一无分支出边前进。
   * **`builtin:array_shift`**：
     * 读取源列表 $A = \text{Eval}(\rho, \text{with.input.source}) = [a_0, a_1, \dots, a_{m-1}]$；
     * 若 $m > 0$（列表非空）：绑定 $\rho(v_{\text{item}}) \leftarrow a_0$、$\rho(v_{\text{rest}}) \leftarrow [a_1, \dots, a_{m-1}]$（新列表副本），控制令牌转入 `.some` 出边；
     * 若 $m = 0$（列表为空）：将当前栈帧中的元素目标变量显式置为未绑定（$\rho(v_{\text{item}}) \leftarrow \text{Unbound}$，消除任何上一轮循环或前置节点的旧值），保持 $\rho(v_{\text{rest}})$ 原状态不变（若 $v_{\text{rest}}$ 尚未绑定则维持 Unbound），控制令牌转入 `.empty` 出边。
   * **`builtin:array_append`**：
     * 读取源列表 $A = \text{Eval}(\rho, \text{with.input.source}) = [a_0, \dots, a_{m-1}]$ 与待追加元素 $x = \text{Eval}(\rho, \text{with.input.item})$；
     * 构造新列表副本 $A' = [a_0, \dots, a_{m-1}, x]$ 并绑定 $\rho(v_{\text{res}}) \leftarrow A'$，控制令牌沿唯一无分支出边前进。
   * **`builtin:map_set`（`OrderedMap` 键序不变式）**：
     * 读取源有序映射 $M = \text{Eval}(\rho, \text{with.input.source}) = [(k_0, u_0), \dots, (k_{m-1}, u_{m-1})]$、目标键 $k = \text{Eval}(\rho, \text{with.input.key})$ 与目标值 $v = \text{Eval}(\rho, \text{with.input.value})$；
     * **键序保持规则**：
       * 若键已存在（$\exists j \in \{0, \dots, m-1\}$ 使得 $k_j = k$）：构造新有序映射副本 $M'$，在原位置 $j$ 处将值更新为 $(k_j, v)$，**保持 $k$ 原有的插入位置不变**；
       * 若键不存在（$k \notin \text{dom}(M)$）：构造新有序映射副本 $M' = [(k_0, u_0), \dots, (k_{m-1}, u_{m-1}), (k, v)]$，将新键值对 $(k, v)$ **追加在遍历序列末尾**；
     * 绑定 $\rho(v_{\text{res}}) \leftarrow M'$，控制令牌沿唯一无分支出边前进。
   * **`builtin:map_get`**：
     * 读取源有序映射 $M = \text{Eval}(\rho, \text{with.input.source}) = [(k_0, u_0), \dots, (k_{m-1}, u_{m-1})]$ 与目标键 $k = \text{Eval}(\rho, \text{with.input.key})$；
     * **未声明 `default` 时**：
       * 若键存在（$\exists j \in \{0, \dots, m-1\}$ 使得 $k_j = k$）：绑定 $\rho(v_{\text{out}}) \leftarrow u_j$，控制令牌转入 `.some` 出边；
       * 若键不存在（$k \notin \text{dom}(M)$）：将当前栈帧中的目标变量显式置为未绑定（$\rho(v_{\text{out}}) \leftarrow \text{Unbound}$，消除任何前置旧值），控制令牌转入 `.none` 出边。
     * **声明了 `default` 时**：
       * 若键存在（$k_j = k$）则令 $y = u_j$，否则令 $y = \text{Eval}(\rho, \text{with.input.default})$；绑定 $\rho(v_{\text{out}}) \leftarrow y$，控制令牌沿唯一无分支出边前进。
   * **`builtin:map_delete`（`OrderedMap` 键序保持删除）**：
     * 读取源有序映射 $M = \text{Eval}(\rho, \text{with.input.source}) = [(k_0, u_0), \dots, (k_{m-1}, u_{m-1})]$ 与目标键 $k = \text{Eval}(\rho, \text{with.input.key})$；
     * 构造移除键 $k$ 后的新有序映射副本 $M' = [(k_i, u_i) \in M \mid k_i \neq k]$，**保持剩余键原有的相对插入顺序不变**（若 $k \notin \text{dom}(M)$，则返回原有序映射的浅拷贝副本）；
     * 绑定 $\rho(v_{\text{res}}) \leftarrow M'$，控制令牌沿唯一无分支出边前进。
   * **`builtin:object_merge`（`TypedDict` 不可变构造与字段覆盖）**：
     * 若声明了 `with.input.base`，读取源结构体并构造浅拷贝副本 $O = \text{Clone}(\text{Eval}(\rho, \text{with.input.base}))$；若未声明 `base`，初始化空记录对象 $O = \{\}$；
     * 对 `with.input` 中除 `base` 外的每个字段名 $f_i \in \{f_1, \dots, f_m\}$，求值 $v_i = \text{Eval}(\rho, \text{with.input}[f_i])$ 并写入副本属性 $O[f_i] \leftarrow v_i$（不原地修改输入快照引用中的旧结构体）；
     * 绑定 $\rho(v_{\text{res}}) \leftarrow O$，控制令牌沿唯一无分支出边前进。
