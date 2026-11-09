# RFC-0015: Axcutor 统一包模型与点分模块寻址（Package & Import System）规范

* **状态**：Implemented
* **所属分类**：语言模块系统、命名空间挂载与编译期可见性检查（Module System, Namespace Mounting & Compile-Time Visibility）
* **前置关联提案**：
  * [`RFC-0011: Axcutor 编译器前后端与 Temporal 运行时参考实现架构`](./0011-compiler-and-temporal-runtime-architecture.md)
  * [`RFC-0014: 基于预置 Python 模块引用的 module 执行器、__axcutor__.yaml 包同构布局与契约模板 SHA 校验规范`](./0014-module-python-executor.md)
* **核心解决的问题**：
  1. **废除带文件后缀与斜杠路径的源文档寻址（`workflow:foo/bar.yaml`），确立全语言统一的逻辑点分包路径（Dotted Package Path）**：将工作流调用（`use: workflow:<pkg_path>`）统一收敛至与宿主文件系统解耦的点分标识符寻址；
  2. **确立“单一文件夹 = 单一工作流包（`__axcutor__.yaml`）”的严格层级正交公理**：消除 Python 中“目录包（Package）与文件模块（Module）同名二义性”，规定工作流的唯一命名单元为包路径，而包内的 `.py` 文件仅作为该包内部私有的 `module` 执行器实现，仅允许通过单点包内相对路径（`.` 或 `.<name>`）绑定；
  3. **引入无遮蔽显式根包挂载表（Explicit Root Mounts）、禁用上级相对逃逸（`..`）与编译期 `_` 前缀硬性私有可见性边界**：从语言机制层面规避 Python `sys.path` 顺序敏感遮蔽、多级相对导入脆弱性以及缺乏真正私有访问控制的问题。

---

## 一、 问题背景与设计动机

### 问题 15.1：文件路径式 `use: workflow:<path>.yaml` 与包寻址模型割裂

在前期规范中，跨工作流调用采用相对文件系统的 POSIX 路径加扩展名格式（如 `use: workflow:billing/verify.yaml`），存在以下结构性缺陷：
1. **暴露物理存储细节**：调用方与被调用方的物理文件扩展名（`.yaml` / `.yml` / `.json`）及目录文件布局强耦合；
2. **与包同构布局不一致**：在 [`RFC-0014`](./0014-module-python-executor.md) 确立“包含 `__axcutor__.yaml` 的文件夹即为工作流包”后，继续使用带文件名的路径寻址会导致同一语言内并存两套异构的引用语法。

### 问题 15.2：直接照搬 Python 原生 `import` 机制会引入已知的工程缺陷

若不加约束地复用 Python 的模块寻址语义，会连带引入以下五个经典问题：
1. **`sys.path` 顺序遍历与同名静默遮蔽（Shadowing）**：按搜索路径列表顺序匹配会导致同名本地目录静默劫持核心库或上游包；
2. **多级父目录相对导入（`..` / `...`）导致重构脆弱与越界**：跨多层父目录的相对导入在目录调整时极易断裂，且容易穿透顶层包边界；
3. **目录包（`foo/__init__.py`）与文件模块（`foo.py`）身份二义性**：同一路径名既可能对应目录也可能对应单文件，并存时产生静默优先级覆盖；
4. **`_` 前缀与 `__all__` 缺乏强制访问控制**：私有模块仅靠约定，外部仍可强行导入内部实现细节；
5. **运行期按序加载导致的循环导入半初始化故障**：运行期边执行顶层语句边解析依赖，在存在环路时产生残缺模块对象。

---

## 二、 核心设计决策

### 决策 1：包与模块身份正交公理（One Directory = One Workflow Package）

Axcutor 严格区分**工作流包（Workflow Package）**与**包内私有 Python 实现模块（Intra-Package Python Module）**两类实体的层级与寻址方式，彻底消除包与文件的同名二义性：

1. **工作流包（Workflow Package）是 `use: workflow:...` 的唯一寻址目标**：
   * 每个具名工作流对应逻辑命名空间中的一个点分包路径 `PackagePath`（如 `billing.charge`），在物理目录树中唯一对应一个包含 `__axcutor__.yaml`（及同级 `__init__.py`）的文件夹 `billing/charge/`；
   * 任何单独的 `.yaml` 或 `.py` 文件名均不再出现在 `use: workflow:<PackagePath>` 的寻址路径中（禁止在 `PackagePath` 中书写 `__axcutor__` 或 `.yaml` / `.json` 后缀）。
2. **包内 `.py` 文件仅作为当前工作流包私有的实现单元**：
   * 工作流包文件夹内部的 `<name>.py` 与 `<name>_contract.py` 严格归属于该工作流包私有，不构成独立的 Axcutor 工作流，不可被 `use: workflow:...` 寻址；
   * 包内 `.py` 模块仅能被其所属工作流包自身の `__axcutor__.yaml` 通过 `run: { executor: "module", content: "<LocalModuleSpec>" }` 以单点包内相对路径引用。

---

### 决策 2：统一的点分寻址文法与禁用上级相对路径（No `..` Traversal）

1. **基础寻址非终结符定义**：
   ```txt
   PackageSegment   ::= Identifier \ R_py ;               (* 合法 ASCII 标识符，排除 Python 35 个硬关键字 *)
   PublicSegment    ::= PackageSegment \ "_"* ;           (* 不以 "_" 开头的公开路径段 *)
   PrivateSegment   ::= "_" ( [a-zA-Z0-9_]* ) ;           (* 以 "_" 开头的私有路径段，且满足 PackageSegment *)

   PackagePath      ::= PackageSegment ( "." PackageSegment )* ;
   ```
2. **工作流引用文法（`use: workflow:<WorkflowRef>`）**：
   * **仅允许从根包起始的绝对点分包路径 `PackagePath`**：
     ```txt
     UseWorkflowSpec  ::= "workflow:" PackagePath ;
     ```
     例如：`use: workflow:billing.charge`、`use: workflow:billing._helper`。
   * **全面禁止相对工作流引用（`.` 或 `..`）与斜杠路径（`/`）**：跨工作流调用一律书写从已挂载根包开始的完整 `PackagePath`。这确保任意 `use: workflow:...` 语句在任何位置含义恒定，移动调用方工作流目录时无需修改其内部任何 `use` 引用，且彻底杜绝 `..` 越过顶层包边界的问题。
3. **包内 Python 模块绑定文法（`run: { executor: "module", content: <LocalModuleSpec> }`）**：
   * **仅允许单点包内相对寻址（`.` 或 `.<LocalPath>`），禁止跨包绝对路径与多点上级路径（`..`）**：
     ```txt
     LocalModuleSpec  ::= "."
                        | "." LocalPath ;

     LocalPath        ::= PackageSegment ( "." PackageSegment )* ;
     ```
     * `"."`：绑定当前工作流包文件夹自身的 `__init__.py`（配合同级 `_contract.py`）；
     * `".<name>"`（如 `".validate_order"`）：绑定当前工作流包文件夹下的 `<name>.py`（配合同级 `<name>_contract.py`）；
     * `".<sub_dir>.<name>"`：绑定当前工作流包文件夹内部子目录下的 `<sub_dir>/<name>.py`。
   * **匿名入口工作流（Anonymous Entry Workflow）的天然隔离推论**：
     当编译请求的入口是一个未挂载在任何具名 Python 包下的匿名内联工作流（例如外部动态提交的根工作流文档 `entry: "__main__"`）时，由于其不存在对应的宿主物理包目录，任何 `run: { executor: "module", ... }` 节点在编译期直接因缺少宿主包上下文而被静态拒绝（报 `module.anonymous_package` 错误）。

---

### 决策 3：显式无遮蔽根包挂载表（Explicit Root Package Mounts）

为规避 Python `sys.path` 顺序遍历导致的同名遮蔽（Shadowing）与隐式路径污染，Axcutor 采用**显式根包挂载表**管理工作流命名空间：

1. **挂载表结构与唯一性公理**：
   * 宿主环境通过不可变的挂载映射 `Mounts: Map<RootPackageName, PackageTree>` 向编译器与运行时提供预置工作流包，其中每个键 `RootPackageName` 必须为单个 `PublicSegment`（如 `"std"`、`"billing"`、`"crm"`）；
   * **严禁同名根包遮蔽**：挂载表中的顶层包名两两互异；若宿主尝试挂载两个同名的顶层包，或同一包路径下同时存在冲突定义，在挂载/编译初始化阶段立即报错拒绝，绝不采用“先匹配者生效”的隐式覆盖策略。
2. **保留入口标识 `__main__`**：
   * 标识符 `"__main__"` 为系统保留的匿名根入口工作流标识（代表不归属于任何已挂载包的独立入口文档）；
   * 已挂载的任何工作流包均不得以 `"__main__"` 作为 `RootPackageName`，且任何工作流均不得在 `use: workflow:...` 中引用 `workflow:__main__`。
3. **编译请求结构（`CompileRequest`）**：
   * `entry`：入口工作流标识，可为 `"__main__"`（独立入口文档）或挂载表中的某个合法公开 `PackagePath`（如 `"billing.order_pipeline"`）；
   * `packages: Ordered<PackagePath, SourceDocument>`：以点分 `PackagePath`（及可选的 `"__main__"`）为键的源文档映射表，完全取代早期的相对文件路径映射。

---

### 决策 4：基于 `_` 前缀的编译期硬性私有可见性边界（Compile-Time Package Visibility）

Axcutor 将路径段中的前导下划线 `_` 定义为**编译器强制执行的包级私有访问控制修饰符**，而非仅靠约定的命名习惯：

1. **公开包与私有包的判定**：
   * 对于任意工作流包路径 $P = s_1 . s_2 . \dots . s_k$（$k \ge 1$）：
     * 若所有路径段 $s_i$ 均不以 `_` 开头（即 $\forall i, s_i \in \text{PublicSegment}$），则 $P$ 为**公开工作流包（Public Workflow Package）**，可被任何工作流及外部入口 `"__main__"` 调用；
     * 若存在某个路径段 $s_m$（$m \ge 2$）以 `_` 开头（即 $s_m \in \text{PrivateSegment}$，例如 `billing._ fraud_check` 或 `billing.checkout._retry_step`），则 $P$ 为归属于其直接父包前缀 $P_{\text{owner}} = s_1 . \dots . s_{m-1}$ 的**内部私有工作流包（Private Workflow Package）**（顶层根包段 $s_1$ 禁止以 `_` 开头）。
2. **编译期可见性校验规则（Pass 3 Visibility Check）**：
   设调用方工作流的标识为 $C$，被调用目标工作流包路径为 $T = s_1 . s_2 . \dots . s_k$：
   * 对 $T$ 中每一个以 `_` 开头的私有段 $s_m$（$2 \le m \le k$），令其所属父包前缀为 $P_{\text{owner}} = s_1 . \dots . s_{m-1}$；
   * **合法调用充要条件**：调用方 $C$ 必须等于 $P_{\text{owner}}$，或 $C$ 是 $P_{\text{owner}}$ 的子包（即 $C$ 以 `$P_{\text{owner}}.$` 为前缀）：
     $$\text{Visible}(C, T) \iff \forall m \in \{2 \dots k\},\; \big(\text{ starts\_with}(s_m, \texttt{"\_"}) \implies (C = P_{\text{owner}} \;\lor\; \text{starts\_with}(C, P_{\text{owner}} \mathbin{\Vert} \texttt{"."})) \big)$$
   * **越权拦截**：
     * 若外部匿名入口工作流（$C = \texttt{"\_\_main\_\_"}$）或不属于 $P_{\text{owner}}$ 树下的其他包（如 $C = \texttt{"reporting.daily"}$）尝试通过 `use: workflow:billing._fraud_check` 调用私有包，编译器在 `use` 字段处直接报告静态错误 **`visibility.private_workflow`**。

---

### 决策 5：两阶段声明式签名解析与无副作用模块加载

1. **工作流级循环引用与顺序无关解析**：
   * 编译器在阶段 1 从 `packages` 闭包中纯静态提取每个工作流包的 `(input, output, branches)` 顶层接口签名表 $\Sigma_{\text{sig}}$，阶段 2~5 仅依据 $\Sigma_{\text{sig}}$ 校验 `use: workflow:<PackagePath>` 的参数与分支绑定；
   * 签名解析完全不依赖包加载顺序，亦不触发任何 Python 模块导入或顶层代码求值。
2. **Python 实现模块的按需绑定与根包命名空间映射**：
   * 在宿主环境中，每个挂载的顶层工作流包 `RootPackageName`（如 `"billing"`）对应一个确定的 Python 包路径（如 `billing` 或宿主配置的挂载前缀 `<host_prefix>.billing`）；
   * 当工作流包 `billing.charge` 中的节点声明 `run: { executor: "module", content: ".verify" }` 时，其目标 Python 模块名唯一确定为 `billing.charge.verify`（当 `content: "."` 时唯一确定为 `billing.charge`），无需遍历搜索路径，亦不存在同名文件与目录的加载歧义。

---

## 三、 完整目录与导入示例

### 1. 宿主挂载的工作流包目录结构

```txt
workflows/                                      # 挂载根：提供顶层包 billing
└── billing/                                    # 包路径：billing (可选包含 __axcutor__.yaml)
    ├── __init__.py
    ├── charge/                                 # 公开工作流包：billing.charge
    │   ├── __init__.py
    │   ├── __axcutor__.yaml
    │   ├── gateway_contract.py                 # [自动生成] 对应 .gateway 的契约模板
    │   └── gateway.py                          # [人工编写] 对应 .gateway 的实现
    └── _risk_score/                            # 私有工作流包：billing._risk_score（仅 billing.* 内部可调）
        ├── __axcutor__.yaml                    # 顶层 run: { executor: "module", content: "." }
        ├── _contract.py                        # [自动生成] 对应 . 的契约模板
        └── __init__.py                         # [人工编写] 对应 . 的实现
```

### 2. 公开工作流包定义（`workflows/billing/charge/__axcutor__.yaml`）

```yaml
input:
  user_id: { type: string }
  amount: { type: number }
output:
  tx_id: { type: string }

graph:
  nodes:
    # 合法：billing.charge 属于 billing 包子树，允许调用同父包下的私有包 billing._risk_score
    check_risk:
      use: workflow:billing._risk_score
      with:
        input:
          user_id: user_id
          amount: amount
        output:
          score: risk_score

    # 合法：通过单点包内相对路径 .gateway 绑定本文件夹下的 gateway.py
    invoke_gateway:
      input: [user_id, amount, risk_score]
      output:
        tx_id: { type: string }
      run:
        executor: module
        content: .gateway

  variables:
    risk_score: { type: number }

  edges:
    - start -> check_risk
    - check_risk -> invoke_gateway
    - invoke_gateway -> end
```

### 3. 外部入口工作流（`entry = "__main__"`）调用边界示例

```yaml
input:
  uid: { type: string }
  price: { type: number }
output:
  receipt_id: { type: string }

graph:
  nodes:
    # 合法：调用公开工作流包 billing.charge
    pay:
      use: workflow:billing.charge
      with:
        input:
          user_id: uid
          amount: price
        output:
          tx_id: receipt_id

    # 若在此处编写 use: workflow:billing._risk_score
    # -> 编译期直接报错 visibility.private_workflow（外部不可访问 _ 私有包）

    # 若在此处编写 run: { executor: module, content: . gateway }
    # -> 编译期直接报错 module.anonymous_package（匿名入口 __main__ 无宿主 Python 包上下文）

  edges:
    - start -> pay
    - pay -> end
```
