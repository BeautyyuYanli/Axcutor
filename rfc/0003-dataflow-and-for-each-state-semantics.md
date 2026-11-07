# RFC-0003: 必达定值分析边界修正与 `for_each.state` 分支/异常状态机语义

* **状态**：Implemented
* **所属分类**：形式化规范完备性与自洽性修正（Formal Specification Soundness & Completeness）
* **核心解决的问题**：明确 `for_each.state` 在多分支输出子集与 `catch` 分支下的编译期约束与运行期回滚语义；修正 `builtin:unwrap` / `builtin:array_shift` 的入口未定值约束；补全 `max_steps` 超限时的并发协同取消语义与 `builtin:*` 运行期规范。
* **关联规范模块**：
  * [`02-type-system-and-symbols/05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md)
  * [`03-static-flow-analysis/03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)
  * [`04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md`](../manual/spec/04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md)
  * [`04-runtime-execution-model/03-guardrails-and-step-accounting.md`](../manual/spec/04-runtime-execution-model/03-guardrails-and-step-accounting.md)
  * [`04-runtime-execution-model/04-exception-handling-and-unwinding.md`](../manual/spec/04-runtime-execution-model/04-exception-handling-and-unwinding.md)

---

## 一、 问题背景与缺陷分析

### 问题 3.1：`for_each.state` 在节点存在多分支输出子集（`branches`）或异常捕获（`catch`）时出现语义悬空
在 [`02-fork-join-and-for-each-scheduler.md`](../manual/spec/04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md) 第 二.2 与 二.3 节中，`for_each.state` 的运行期行为被描述为：
> “按 $i = 0, 1, \dots, n-1$ 串行执行，并将第 $i-1$ 步更新后的 `state` 传入第 $i$ 步……将第 $n-1$ 次迭代结束后的最终累积状态写入外层 `<state_var>`。”

然而，挂载了 `for_each`（且声明了 `state: [s]`）的节点 $M$ 允许同时声明 `branches`（不同分支产出不同的 `output` 子集）和 `catch`（根据规范，`catch` 分支的产出子集为空集 $\emptyset$）：

1. **正常分支漏产 `state` 变量**：
   若节点 $M$ 声明了分支 `skip: { description: "...", output: [] }`（未包含状态变量 `s`），当第 $i$ 次迭代走向 `skip` 分支时，$\text{out}_i$ 中根本不存在键 `s`！此时第 $i+1$ 次迭代读取的 `s` 是 `undefined` 还是沿用第 $i-1$ 步的值？现行规范既未在编译期禁止正常分支漏产 `state` 变量，也未在运行期定义缺失时的回退规则。
2. **`catch` 分支下的 `state` 连续性**：
   若节点 $M$ 挂载了 `for_each`（含 `state: [s]`）并配置了元素级异常隔离 `catch: { bad_item: "*" }`，当第 $i$ 个元素触发异常进入 `bad_item` 分支时，该次尝试的脏状态被回滚（产出 $\text{out}_i = \emptyset$）。规范需明确此时累积状态 `s` 不得丢失，而应原样保留第 $i-1$ 次迭代完成时的有效快照值传入第 $i+1$ 次迭代。

---

### 问题 3.2：`builtin:unwrap` 与 `builtin:array_shift` 的“无预设初值约束”存在定值分析漏洞
在 [`05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md) 中：
* 对未声明 `default` 的 `builtin:unwrap`：规定“禁止 $v_{\text{out}} \in \text{dom}(G.\text{variables})$”；
* 对 `builtin:array_shift`：规定“禁止 $v_{\text{item}} \in \text{dom}(G.\text{variables})$”。

这一规则试图防止在 `.none` 或 `.empty` 分支（其 `output_subset = \emptyset`）上，由于目标变量此前已有初值而导致下游在 `.none` / `.empty` 路径上误读旧值。但仅检查 $v \notin \text{dom}(G.\text{variables})$ 存在两个逻辑漏洞：

1. **漏检了宿主输入 $\text{HostIn}(G)$ 与前序节点定值**：
   * 若 $v_{\text{out}} \in \text{HostIn}(G)$（例如作为顶层或子图 `input` 传入），或者在 `unwrap` / `array_shift` 节点之前已有另一个节点对 $v_{\text{out}}$ 赋过值，则 $v_{\text{out}} \notin \text{dom}(G.\text{variables})$ 依然成立（通过了该检查），但在进入 `unwrap` 节点时 $v_{\text{out}} \in Def_{\text{in}}(N)$！
   * 于是根据 [`03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md) 的传递方程 $Def_{\text{out}}(N.\text{none}) = Def_{\text{in}}(N) \cup \emptyset = Def_{\text{in}}(N)$，在 `.none` 分支上 $v_{\text{out}}$ 仍然被判定为“已必达定值”，下游在 `.none` 分支上读取 $v_{\text{out}}$ 将不会报错并读到旧值！
2. **限制了同一图内顺序两个独立循环复用迭代变量名**：
   * 若图中先后有两个串行的 `while` 队列消费循环（Loop 1 消费完后进入 Loop 2），两者使用不同的 `array_shift` 节点，若想在 `.empty` 分支上真正消除旧值可见性，应当通过数据流传递方程中的 **Kill 集（反定值）** 或入口未定值断言来表达。

---

### 问题 3.3：`max_steps` 超限中断图栈帧时缺少在途并发任务的协同取消规定
* 在 [`03-guardrails-and-step-accounting.md`](../manual/spec/04-runtime-execution-model/03-guardrails-and-step-accounting.md) 第 一.2 节中：
  > “每当 $\text{steps}(F_G) > G.\text{max\_steps}$ 时，立即拒绝激活新节点，中断当前图栈帧 $F_G$：若 $F_G$ 为子图栈帧，销毁并回滚 $F_G$，向父节点边界抛出可恢复异常 `max_steps`”
* 考虑并发 `for_each`（如 `concurrency: 8`，源列表长度 $n = 100$，当前图剩余步数额度为 $5$）：前 5 个元素启动后，当准备启动第 6 个元素时触发 $\text{steps}(F_G) > G.\text{max\_steps}$。此时前 5 个元素仍在并发执行中！规范未说明在销毁 $F_G$ 并向上抛出 `max_steps` 之前，必须向当前栈帧 $F_G$ 内所有在途并发任务（Fork-Join 成员或 `for_each` 在途迭代）发送 `CancelledError`（触发 `abort_signal`）并等待其退出。

---

### 问题 3.4：内建算子（`builtin:*`）在 `04-runtime-execution-model/` 中缺失运行期状态机语义
* 5 个内建算子（`unwrap`、`switch`、`array_shift`、`array_append`、`map_set`）仅在 `02-type-system-and-symbols/05-builtin-generic-inference.md` 中定义了**编译期类型推导**，而在 `04-runtime-execution-model/` 中未定义其**运行期求值语义与计步规则**：
  * `builtin:*` 节点激活时是否计入 `steps(F_G) += 1`？（应明确：计 1 步，确保由纯 `builtin:*` 构成的死循环同样受 `max_steps` 护栏保护）。
  * `builtin:map_set` 对已存在的 `key` 写入新值时，在 `OrderedMap` 中的键顺序如何变化？（应明确：遵循标准有序映射语义，覆盖已有键的值时**保持该键原有的插入位置不变**；仅当键不存在时追加至末尾）。
  * `builtin:array_shift`、`builtin:array_append`、`builtin:map_set` 均为纯函数式不可变更新（Copy-on-Write），不原地修改输入快照引用。

---

## 二、 解决方案与规范修订设计

### 方案 3.1：形式化定义 `for_each.state` 的编译期分支穷尽约束与运行期状态传递方程

1. **编译期分支完备性断言**（增补至 [`03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md)）：
   * 对于任意挂载了 `for_each` 且声明了非空 `for_each.state` 的节点 $M$：
     * 若 $M$ 未声明 `branches`：必须满足 $\text{set}(M.\text{for\_each.state}) \subseteq \text{dom}(\text{SingleOut}(M))$；
     * 若 $M$ 声明了非空 `branches`：对 $M$ 的**每一个正常业务分支** $b \in \text{dom}(M.\text{branches})$，其承诺产出的字段子集必须包含全部 `state` 变量：
       $$\forall b \in \text{dom}(M.\text{branches}), \quad \text{set}(M.\text{for\_each.state}) \subseteq \text{set}(M.\text{branches}[b].\text{output})$$
       *(注：若某正常分支无需改变某个状态变量 $s$，在内联 `graph` 中由于 $s \in \text{HostIn}(M)$ 已在子图入口定值，子图直接走向 `end.<branch>` 即可自动将原值 $s$ 作为输出提交；在 `run` 节点中则包含在 `output` 中返回。)*

2. **运行期 `for_each.state` 迭代传递方程**（增补至 [`02-fork-join-and-for-each-scheduler.md`](../manual/spec/04-runtime-execution-model/02-fork-join-and-for-each-scheduler.md)）：
   * 设进入节点 $M$ 前外层栈帧中状态变量的初始映射为 $\sigma_0 = \rho_{\text{outer}}|_{M.\text{for\_each.state}}$；
   * 对于第 $i = 0, 1, \dots, n-1$ 次串行迭代（以 $\sigma_i$ 作为输入状态快照）：
     * 若第 $i$ 次迭代**正常完成**（返回正常分支 $b_i$ 与输出 $\text{out}_i$）：
       $$\sigma_{i+1} = \text{out}_i|_{M.\text{for\_each.state}}$$
     * 若第 $i$ 次迭代**被 `M.catch` 捕获**（进入异常分支 $c_i$，当次迭代脏状态回滚）：
       $$\sigma_{i+1} = \sigma_i \quad \text{（保留上一轮有效状态不变）}$$
   * 当全部 $n$ 次迭代结束时，将最终状态 $\sigma_n$ 原子写回外层图栈帧 $\rho_{\text{outer}}$。

---

### 方案 3.2：在必达定值传递方程中引入分支 Kill 集，统一修正 `unwrap` 与 `array_shift` 约束

将 [`05-builtin-generic-inference.md`](../manual/spec/02-type-system-and-symbols/05-builtin-generic-inference.md) 中针对 `G.variables` 的特判移除，改为在 [`03-branch-sensitive-definite-assignment.md`](../manual/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment.md) 的边级传递方程中引入 `KillVars(N, b)`（或在算子入口断言未定值）：

* **通用分支边传递方程**：
  对于节点 $N$ 的分支 $b$（含 `.none`、`.empty` 及 `catch` 分支），定义该节点声明但未在分支 $b$ 中产出的独占输出变量集为杀死集（或对 `builtin:unwrap` 的 `v_out` 与 `builtin:array_shift` 的 `v_item`）：
  * 在 `builtin:unwrap`（无 `default`）的 `.none` 分支上：
    $$Def_{\text{out}}(N.\text{none}) = Def_{\text{in}}(N) \setminus \{v_{\text{out}}\}$$
    且在运行期进入 `.none` 分支时，将当前栈帧中的 $\rho(v_{\text{out}})$ 置为未绑定（Unbound）；
  * 在 `builtin:array_shift` 的 `.empty` 分支上：
    $$Def_{\text{out}}(N.\text{empty}) = Def_{\text{in}}(N) \setminus \{v_{\text{item}}\}$$
    且在运行期进入 `.empty` 分支时，将当前栈帧中的 $\rho(v_{\text{item}})$ 置为未绑定（Unbound）。

> **优势**：
> 1. 彻底堵住了 $v_{\text{out}} \in \text{HostIn}(G)$ 或前序节点定值在 `.none` / `.empty` 分支上泄露旧值的漏洞；
> 2. 允许同一张图中前后两个独立的 `array_shift` 循环或两次 `unwrap` 合法复用同一个局部变量名 `item`（只要在 `.empty` / `.none` 之后重新定值前不读取它）。

---

### 方案 3.3：补全 `max_steps` 超限协同取消协议与 `builtin:*` 运行期语义

1. **`max_steps` 超限时的在途并发清理**（修订 [`03-guardrails-and-step-accounting.md`](../manual/spec/04-runtime-execution-model/03-guardrails-and-step-accounting.md)）：
   * 当图栈帧 $F_G$（含并发 `for_each` 激活新元素迭代时）检测到 $\text{steps}(F_G) > G.\text{max\_steps}$ 时，立即停止派发新任务，向 $F_G$ 树下所有尚未完成的并发分支/迭代及子栈帧广播不可捕获的 `CancelledError`（触发底层执行器 `abort_signal`），等待全部在途任务退出后销毁 $F_G$，再向父节点边界抛出 `max_steps` 异常（或根图抛出 `MaxStepsExceededError`）。
2. **`builtin:*` 运行期规范**（增补至 `04-runtime-execution-model/`）：
   * **计步规则**：每次激活 `use: builtin:<op>` 节点同样执行 $\text{steps}(F_G) \leftarrow \text{steps}(F_G) + 1$。
   * **纯函数式值语义**：`array_shift`、`array_append`、`map_set` 均返回新构造的集合副本（或结构共享不可变持久化数据结构），绝不原地修改当前栈帧快照中的旧集合。
   * **`OrderedMap` 键序不变式**：`builtin:map_set` 在键 $k \in \text{dom}(M)$ 已存在时，原地更新该键关联的值并保留 $k$ 的原始插入次序；在 $k \notin \text{dom}(M)$ 时，将 $(k, v)$ 追加在遍历序列末尾。
