# 03. 全路径分支敏感必达定值分析（Definite Assignment Analysis）

为确保运行时不发生读取未初始化变量（Unassigned Variable Read）的错误，静态检查器对每张图 $G$ 独立执行前向必达数据流分析（Forward Must-Analysis）。

---

## 一、 数据流格（Lattice）与起点初始化

* **数据流论域**：幂集格 $(\mathcal{P}(\text{dom}(\Sigma_G)), \subseteq)$，其中交汇算子（Meet Operator）为集合交集 $\bigcap$。
* **起点出边初始定值集 $Def_{\text{in}}(\text{start})$**：
  $$Def_{\text{in}}(\text{start}) = \text{HostIn}(G) \cup \text{dom}(G.\text{variables})$$
  *(注：仅在宿主 `output` 中声明、但既不属于 $\text{HostIn}(G)$ 也未在 $G.\text{variables}$ 中赋初值的变量，在 `start` 处不属于 $Def_{\text{in}}(\text{start})$。)*

---

## 二、 边级定值传递方程（Edge Transfer Functions）

对于图内节点 $N$，设其入口定值集为 $Def_{\text{in}}(N)$，则从 $N$（或 `start`）发出的各类控制边 $e$ 的出口定值集 $Def_{\text{out}}(e)$ 定义如下：

1. **从 `start` 发出的边**：
   * 若为单跳边 $e = (\text{start} \to \text{target})$：$Def_{\text{out}}(e) = Def_{\text{in}}(\text{start})$；
   * 若为并发边 $e = (\text{start} \to [W_1, \dots, W_k] \to \text{join})$：每个并发分支入口定值集 $Def_{\text{in}}(W_i) = Def_{\text{in}}(\text{start})$。
2. **无分支普通节点或挂载了 `for_each` 的节点 $N$ 的默认出边 $e = (N \to \text{target})$**：
   $$Def_{\text{out}}(e) = Def_{\text{in}}(N) \cup OutVars(N)$$
   其中：
   * 若 $N$ 未挂载 `for_each`：$OutVars(N)$ 为 $N$ 的全部输出目标变量集合；
   * 若 $N$ 挂载了 `for_each`：$OutVars(N)$ 为 $N.\text{for\_each}$ 中 `filter`、`collect` 与 `state` 声明的全部外层目标变量集合。
3. **未挂载 `for_each` 的节点 $N$ 的正常业务分支出边 $e_b = (N.b \to \text{target})$**：
   定义分支 $b$ 的**反定值杀死集（Kill Set）** $KillVars(N, b)$：
   * 当 $N$ 为未声明 `default` 的 `builtin:unwrap` 或未声明 `default` 的 `builtin:map_get`（输出绑定为 $v_{\text{out}}$）且 $b = \texttt{none}$ 时：$KillVars(N, \texttt{none}) = \{v_{\text{out}}\}$；
   * 当 $N$ 为 `builtin:array_shift`（元素输出绑定为 $v_{\text{item}}$）且 $b = \texttt{empty}$ 时：$KillVars(N, \texttt{empty}) = \{v_{\text{item}}\}$；
   * 其余所有节点与分支：$KillVars(N, b) = \emptyset$。

   通用分支边定值传递方程为先移除 $KillVars(N, b)$，再并入分支 $b$ 契约承诺产出的输出变量子集 $OutVars(N, b)$：
   $$Def_{\text{out}}(e_b) = (Def_{\text{in}}(N) \setminus KillVars(N, b)) \cup OutVars(N, b)$$
   即特化为：
   * 在 `builtin:unwrap`（无 `default`）或 `builtin:map_get`（无 `default`）的 `.none` 分支上（$OutVars(N, \texttt{none}) = \emptyset$）：
     $$Def_{\text{out}}(N.\text{none}) = Def_{\text{in}}(N) \setminus \{v_{\text{out}}\}$$
     *(注：运行期进入 `.none` 分支时，同步将当前栈帧中的 $\rho(v_{\text{out}})$ 置为未绑定 Unbound；既防止 $v_{\text{out}} \in \text{HostIn}(G)$ 或前序节点定值在 `.none` 分支上泄露旧值，也允许同一张图中多个独立的 `unwrap` 或 `map_get` 节点合法复用同一个变量名 $v_{\text{out}}$。)*
   * 在 `builtin:array_shift` 的 `.empty` 分支上（$OutVars(N, \texttt{empty}) = \emptyset$）：
     $$Def_{\text{out}}(N.\text{empty}) = Def_{\text{in}}(N) \setminus \{v_{\text{item}}\}$$
     *(注：运行期进入 `.empty` 分支时，同步将当前栈帧中的 $\rho(v_{\text{item}})$ 置为未绑定 Unbound；允许同一张图中前后多个独立的 `array_shift` 循环合法复用同一个迭代变量名 $v_{\text{item}}$。)*
4. **未挂载 `for_each` 的节点 $N$ 的异常捕获出边 $e_c = (N.c \to \text{target})$**（$c \in \text{dom}(N.\text{catch})$）：
   异常分支回滚局部产出（$OutVars(N, c) = \emptyset$）：
   $$Def_{\text{out}}(e_c) = Def_{\text{in}}(N)$$
5. **Fork-Join 并发三元边 $e_{\text{fork}} = (\text{src} \to [W_1, \dots, W_k] \to \text{join\_target})$**：
   设触发源端点 $\text{src}$（可为 `start`、无分支节点 $S$ 或分支端点 $S.b$）对应的源出边定值集为 $Def_{\text{src}}$，则：
   * 每个并发成员节点的入口定值集为：$Def_{\text{in}}(W_i) = Def_{\text{src}}$（$\forall i \in \{1, \dots, k\}$）；
   * Join 屏障汇合后进入 $\text{join\_target}$ 的边出口定值集为：
     $$Def_{\text{out}}(e_{\text{fork}}) = Def_{\text{src}} \cup \bigcup_{i=1}^{k} OutVars(W_i)$$

---

## 三、 汇合方程（Meet Equation）与不动点求解

对于图内除 `start` 与并发组成员外的任意节点或终态端点 $X \in \text{dom}(G.\text{nodes}) \cup \{\text{end}, \text{end}.*, \text{fail}\}$：
$$Def_{\text{in}}(X) = \bigcap_{e \in \text{InEdges}(X)} Def_{\text{out}}(e)$$

由于图可能包含成环回边，算法按标准必达分析（Must-Analysis）初始化：
* 令 $Def_{\text{in}}(\text{start}) = \text{HostIn}(G) \cup \text{dom}(G.\text{variables})$；
* 令所有其他节点 $X$ 的初始 $Def_{\text{in}}^{(0)}(X) = \text{dom}(\Sigma_G)$（全集 $\top$）；
* 迭代应用上述传递方程与交集汇合方程，由于有限幂集格单调递减，必在有限步内收敛至最大不动点（Greatest Fixed Point）$Def_{\text{in}}^*(X)$。

---

## 四、 节点读取、图出口与 `for_each.state` 分支完备性的编译期定值断言

在求得不动点 $Def_{\text{in}}^*(X)$ 后，执行以下三项断言：

### 1. 节点入口读取集断言（Entry Use Assertion）
对每个节点 $N \in \text{dom}(G.\text{nodes})$，定义其外层图视角的顶层读取变量集 $Use(N)$：
* 若 $N$ 为 `ActionNode`（`run`）：
  * 未挂载 `for_each` 时：$Use(N) = \text{set}(N.\text{input})$；
  * 挂载 `for_each` 时：$Use(N) = (\text{set}(N.\text{input}) \setminus \text{Injected}(N.\text{for\_each})) \cup \{\text{RootVar}(N.\text{for\_each.source})\} \cup \text{set}(N.\text{for\_each.state})$。
* 若 $N$ 为 `InlineGraphNode`（内联 `graph`）：
  $$Use(N) = \text{set}(N.\text{input}) \cup \{\text{RootVar}(N.\text{for\_each.source}) \text{ (若有)}\} \cup \text{set}(N.\text{for\_each.state} \text{ (若有)})$$
* 若 $N$ 为 `CallNode`（`use`）：
  * 令 $R_{\text{with}}$ 为 $N.\text{with.input}$（含默认同名绑定的参数）中所有变量/路径引用的根变量集合；
  * 未挂载 `for_each` 时：$Use(N) = R_{\text{with}}$；
  * 挂载 `for_each` 时：$Use(N) = (R_{\text{with}} \setminus \text{Injected}(N.\text{for\_each})) \cup \{\text{RootVar}(N.\text{for\_each.source})\} \cup \text{set}(N.\text{for\_each.state})$。

**定值断言**：必须满足 $Use(N) \subseteq Def_{\text{in}}^*(N)$。若存在 $v \in Use(N) \setminus Def_{\text{in}}^*(N)$，编译期报错。

### 2. 图终态出口定值断言（Exit Commit Assertion）
* **无分支终点 `end`**：必须满足 $\text{dom}(\text{host.output}) \subseteq Def_{\text{in}}^*(\text{end})$。
* **分支终点 `end.<branch>`**：必须满足 $\text{set}(\text{host.branches}[\text{branch}].\text{output}) \subseteq Def_{\text{in}}^*(\text{end.}\text{branch})$。
* **异常回滚终点 `fail`**：无定值要求（豁免检查）。

### 3. `for_each.state` 编译期分支完备性断言（State Branch Completeness Assertion）
对于任意挂载了 `for_each` 且声明了非空 `for_each.state` 的节点 $M$，为保证每一次正常完成的串行迭代均能向下一轮迭代提供完整的累积状态变量：
* 若 $M$ 未声明 `branches`（对 `CallNode` 指被调用工作流顶层未声明 `target.branches`）：必须满足 $\text{set}(M.\text{for\_each.state}) \subseteq \text{dom}(\text{SingleOut}(M))$；
* 若 $M$ 声明了非空 `branches`：对 $M$ 的**每一个正常业务分支** $b \in \text{dom}(M.\text{branches})$，其承诺产出的字段子集必须包含全部 `state` 变量：
  $$\forall b \in \text{dom}(M.\text{branches}), \quad \text{set}(M.\text{for\_each.state}) \subseteq \text{set}(M.\text{branches}[b].\text{output})$$
  若存在任何正常业务分支遗漏了某个 `state` 变量，静态检查器在编译期报错。
  *(注：若某正常分支无需改变某个状态变量 $s$，在内联 `graph` 中由于 $s \in \text{HostIn}(M)$ 已在子图入口定值，子图直接走向 `end.<branch>` 即可自动将原值 $s$ 作为输出提交；在 `run` 节点中则包含在 `output` 中原样返回。)*
