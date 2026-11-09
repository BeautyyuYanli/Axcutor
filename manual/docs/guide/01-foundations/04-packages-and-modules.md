# 04. 包挂载与 Python 模块

具名工作流以绝对点分包名寻址，每个包对应含 `__axcutor__.yaml` 和 `__init__.py` 的目录。包内 Python 实现由 Worker 预先安装，通过 `module` 执行器调用。

## 包布局与寻址

```text
workflows/
└── billing/                 # 显式挂载的公开根包
    ├── __init__.py
    └── charge/
        ├── __axcutor__.yaml # 工作流包 billing.charge
        ├── __init__.py
        ├── gateway.py      # 手写业务实现
        └── gateway_contract.py # 自动生成，不手工修改
```

调用工作流使用完整包名；Python 实现仅使用当前包内相对路径：

```yaml
use: workflow:billing.charge
```

```yaml
run: {executor: module, content: .gateway}
```

| `content` | Python 目标（默认根映射） | 生成契约路径 |
| --- | --- | --- |
| `.` | `billing.charge` | `billing/charge/_contract.py` |
| `.gateway` | `billing.charge.gateway` | `billing/charge/gateway_contract.py` |
| `.sub.gateway` | `billing.charge.sub.gateway` | `billing/charge/sub/gateway_contract.py` |

路径段为 ASCII 标识符，排除 Python 硬关键字。工作流包根不能以 `_` 开头；包名不能包含 `__axcutor__`，不能使用 `.yaml`、`.yml`、`.json` 文件后缀。工作流引用禁止斜杠、`.`、`..` 相对寻址和 `workflow:__main__`。`module.content` 仅允许 `.` 或单个前导点后的局部路径，禁止绝对模块名与上级跳转。

独立 YAML/JSON 入口文件映射为 `__main__`。匿名入口没有 Python 包上下文，因此不能直接包含 `module` 节点，但可调用公开工作流包。

## 显式挂载与可见性

```sh
# 根名与目录显式配对；可重复 --mount 挂载不同根。
uv run --locked axcutor run workflows/billing/charge --mount billing=workflows/billing
```

同一根名重复出现、物理根重叠、包目录与同名 `.py` 并存均被拒绝。目录中的命名空间父包可省略 `__axcutor__.yaml`，但必须含 `__init__.py`。发现与编译仅读取工作流文档，不导入或执行任何 Python 初始化代码。

私有可见性按每个 `_` 开头的段分别判断：

| 调用方 | 目标 | 结果 |
| --- | --- | --- |
| `billing.checkout` | `billing._risk` | 允许，调用方位于 `billing` 子树 |
| `reporting.daily` 或 `__main__` | `billing._risk` | 拒绝：`visibility.private_workflow` |
| `billing.checkout` | `billing._risk.tools._detail` | 拒绝，还需位于 `billing._risk.tools` 子树 |

外部入口只能选择所有段均公开的包或 `__main__`。签名表在检查各包实现前收集完毕，循环引用不依赖 `packages` 的声明顺序。

## 生成契约与编写实现

下面是 `billing/charge/__axcutor__.yaml` 的完整原子工作流：

```yaml
# 输入输出决定生成模型及契约指纹。
input:
  amount: {type: number}
output:
  receipt: {type: string}
run:
  executor: module
  content: .gateway # 绑定本包 gateway.py
```

```sh
# 使用归一化后的编译契约生成同级模板，不导入 gateway.py。
uv run --locked axcutor contracts workflows/billing/charge --mount billing=workflows/billing

# CI 可检查缺失或过期契约；此模式不写文件。
uv run --locked axcutor contracts workflows/billing/charge --mount billing=workflows/billing --check
```

业务实现 `gateway.py` 导入同一模板的契约类型，声明顶层异步入口：

```python
from .gateway_contract import Input, Output, ReturnType

async def execute(input: Input) -> ReturnType:
    return Output(receipt=f"paid:{input.amount:.2f}")
```

有分支时导入 `Input`、`Branch`、`ReturnType`，返回 `Branch.<分支>(...)`。嵌套输出结构体需显式构造生成的 `Output_<path>`；可选键的省略与 `null` 含义不同。详细类型映射与 `builtin` 共用 [Python 契约模型规则](../../../spec/05-executor-abi/02-builtin-python-executor.md)。

模板只包含契约与模块级 `__axcutor_contract_sha256__`，不包含业务函数。指纹覆盖归一化的输入、输出与分支，包括声明顺序。多个引用指向同一实际实现文件时，全部契约必须一致，包括 `.` 与 `.__init__`。模板目标与已有 Python 包目录同名、覆盖实现文件或共享实现契约不一致时，生成与 `--check` 均在写入前拒绝。模板变更后应重新生成并随业务包部署。

## Worker 绑定与生命周期

默认逻辑根 `billing` 映射到已安装 Python 包 `billing`。宿主也可显式配置前缀：

```sh
# 此时 billing.charge 的 .gateway 加载 company.billing.charge.gateway。
uv run --locked axcutor worker --module-mount billing=company.billing
```

显式 Worker 映射为封闭表，缺少根映射会产生 `execution` 错误。重复根名、同一 Python 根的别名与重叠映射被拒绝。该配置只由宿主提供，不能通过 HTTP 提交覆写。目录挂载名、契约生成位置与 Worker 映射必须指向同一部署包；本地可通过安装包或设置 `PYTHONPATH` 使其可导入。实现路径的末段必须为文件模块，`.` 指向包；同名包目录与 `.py` 文件并存会被拒绝。

每次尝试按以下边界执行：

1. 导入目标模块并确认 `execute` 为协程函数。
2. 确认 `Input` 与 `Output` 或 `Branch` 来自同一生成契约模块，并比较 SHA。
3. 以深拷贝快照构造冻结输入模型，转换传输层数字；列表、映射保持 Python 容器语义，其修改不会影响原快照。
4. 执行业务协程，严格核对输出类型与分支，再交给 Rust 进行最终输出校验。
5. 取消或超时时取消业务任务并等待清理完成，丢弃迟到结果。

导入失败和普通业务异常归为 `execution`，保留物理 Python 文件与行号；契约失配、返回类型错误及本契约输出模型的校验错误归为 `contract`。业务自建 Pydantic 模型的错误仍归为 `execution`。

工作流文档在提交时形成不可变快照；Python 模块使用宿主部署与导入缓存，代码版本由宿主管理，不嵌入执行历史。进行中的执行需要匹配快照 SHA 的契约，不能通过修改模板跳过部署一致性。

[Playground](../00-web-playground.md) 使用一个常驻 Worker，在两次 Run 之间清除 workspace 自有模块与字节码缓存，再普通导入当前文件。例如，当前执行清理完成后把 `value * 2` 改成 `value * 3` 并保存，下一次 Run 导入新文件。提交、运行和清理期间禁止文件编辑、新建、删除、保存和再次 Run；后端在忙时拒绝保存和新 Run，返回 `409`。单次图内并发保持原样。此边界由独立 Playground 后端负责，通用 `module` 执行器仍使用 Python 标准导入缓存。

仓库的 `examples/workflows/demo/module_double/` 提供可运行示例：

```sh
uv run --locked axcutor contracts examples/workflows/demo/module_double --mount demo=examples/workflows/demo --check
# 在独立终端安装或暴露预置业务包，然后启动 Worker。
PYTHONPATH=examples/workflows uv run --locked axcutor worker --module-mount demo=demo
```
