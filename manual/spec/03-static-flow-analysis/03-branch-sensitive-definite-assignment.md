# 03. 全路径分支敏感必达定值分析（Definite Assignment Analysis）

为确保运行时绝不发生读取未初始化变量（Unassigned Variable Read）的错误，静态检查器对每张图 $G$ 独立执行前向必达数据流分析（Forward Must-Analysis）。

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
   仅并入分支 $b$ 契约承诺产出的输出变量子集 $OutVars(N, b)$：
   $$Def_{\text{out}}(e_b) = Def_{\text{in}}(N) \cup OutVars(N, b)$$
4. **未挂载 `for_each` 的节点 $N$ 的异常捕获出边 $e_c = (N.c \to \text{target})$**（$c \in \text{dom}(N.\text{catch})$）：
   异常分支回滚一切局部产出（$OutVars(N, c) = \emptyset$）：
   $$Def_{\text{out}}(e_c) = Def_{\text{in}}(N)$$
5. **Fork-Join 并发三元边 $e_{\text{fork}} = (\text{src} \to [W_1, \dots, W_k] \to \text{join\_target})$**：
   设触发源端点 $\text{src}$（可为 `start`、无分支节点 $S$ 或分支端点 $S.b$）对应的源出边定值集为 $Def_{\text{src}}$，则：
   * 每个并发成员节点的入口定值集为：$Def_{\text{in}}(W_i) = Def_{\text{src}}$（$\forall i \in \{1, \dots, k\}$）；
   * Join 屏障汇合后进入 $\text{join\_target}$ 的边出口定值集为：
     $$Def_{\text{out}}(e_{\text{fork}}) = Def_{\text{src}} \cup \bigcup_{i=1}^{k} OutVars(W_i)$$

---

## 三、 汇合方程（Meet Equation）与不动点求解

对于图内除 `start` 與并发组成员外的任意节点或终态端点 $X \in \text{dom}(G.\text{nodes}) \cup \{\text{end}, \text{end}.*, \text{fail}\}$：
$$Def_{\text{in}}(X) = \bigcap_{e \in \text{InEdges}(X)} Def_{\text{out}}(e)$$

由于图可能包含成环回边，算法按标准必达分析（Must-Analysis）初始化：
* 令 $Def_{\text{in}}(\text{start}) = \text{HostIn}(G) \cup \text{dom}(G.\text{variables})$；
* 令所有其他节点 $X$ 的初始 $Def_{\text{in}}^{(0)}(X) = \text{dom}(\Sigma_G)$（全集 $\top$）；
* 迭代应用上述传递方程与交集汇合方程，由于有限幂集格单调递减，必在有限步内收敛至最大不动点（Greatest Fixed Point）$Def_{\text{in}}^*(X)$。

---

## 四、 节点读取与图出口的编译期定值断言

在求得不动点 $Def_{\text{in}}^*(X)$ 后，执行以下两项断言：

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
