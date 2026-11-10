# 05. 资源句柄

`handle` 在工作流内部传递受管 Python 对象的身份。对象保存在 Worker 中；工作流变量只保存句柄，框架按实际引用安排回收。

## 类型与宿主边界

```yaml
# kind 使用逻辑资源包路径和公开顶层资源类名。
tx: {type: handle, kind: infra.postgres:Tx}
file: {type: [handle, "null"], kind: infra.s3:TempFile}
```

`handle` 只允许 `type`、`kind` 和可选 `description`。资源包私有段遵守工作流包可见性。同 kind 的类型才兼容；容器和可空性沿用现有规则。路径段禁止 `__axcutor__` 与 `__ax_resources__`。

资源包包含 `__init__.py` 和独立的 `__ax_resources__.yaml`，可与同路径工作流共存。声明局部示例：

```yaml
# infra/postgres/__ax_resources__.yaml
resources:
  Tx: {description: 数据库事务}
  Conn:
```

头文件只允许 `resources`，其值为非空映射。键为公开 Python 包标识符；每项为 `null` 或仅含可选字符串 `description` 的映射。编译器检查所有挂载的资源头文件，递归核对 input、output 和图变量中的 kind。未声明时报 `type.unknown_resource`，私有资源越界时报 `visibility.private_resource`。编译过程不导入 Python，声明不替代 Worker 上的类协议校验。

句柄可经 `assign`、容器算子和 `unwrap` 传递。同 kind 句柄支持 `compare` 的 `eq/ne`，不支持字段投影、算术、逻辑或有序比较。非空句柄不能由 YAML 字面量创建；可空句柄的 `null` 和句柄容器的空值合法：

```yaml
variables:
  current: {type: [handle, "null"], kind: infra.postgres:Tx, default: null}
  pending: {type: array, items: {type: handle, kind: infra.postgres:Tx}, default: []}
```

宿主根入口的 input/output 递归禁止句柄，包括可空句柄和句柄容器。含句柄接口的定义可以编译、生成契约和被内部调用。`signal` 的 output 同样禁止句柄；input 只作为依赖，不解引用。HTTP、CLI 和宿主结果继续使用纯 JSON 值。

## 创建与借用

资源类由对应资源包的 `__init__.py` 公开导出，直接使用 `@resource` 或 `@resource(persistent=True)`。类可定义在该初始化文件或同目录的单文件子模块（包括 `_tx.py`），禁止从其他包重导出。公开接口只有 `await resources.put(instance)` 和 `await resources.get(handle)`。

```python
# infra/counter/__init__.py：此对象只保存进程内状态，不能在其他 Worker 恢复。
from axcutor_runtime import resource

@resource
class Counter:
    def __init__(self, value: int) -> None:
        self.value = value

    async def dispose(self) -> None:
        self.value = 0
```

```python
# 创建节点：Output.counter 声明为 handle，kind 为 infra.counter:Counter。
from axcutor_runtime import resources
from infra.counter import Counter
from .create_contract import Input, Output, ReturnType

async def execute(input: Input) -> ReturnType:
    return Output(counter=await resources.put(Counter(input.value)))
```

```python
# 消费节点：get 返回精确的 Counter 类型，只借用到本次尝试实际排空。
from axcutor_runtime import resources
from .read_contract import Input, Output, ReturnType

async def execute(input: Input) -> ReturnType:
    counter = await resources.get(input.counter)
    return Output(value=counter.value)
```

`put` 校验完成后才接管实例；此前的清理责任属于业务。同一 Python 实例不能重复注册。同一底层连接、事务、流或外部删除单元也不能通过其他包装重复注册，后者由资源类与调用者保证。

`get` 仅接受公共 `Handle[T]`，并核对本次输入或本次分配的授权。手工构造信封不会获得权限。资源钩子内不能调用 `resources`；借出的对象不能通过全局变量或游离任务跨尝试使用。

## 恢复与析构

| 资源类型 | 类协议 | 使用与回收 |
| --- | --- | --- |
| 瞬态 | 无定位协议或 locator 为 `None`，`persistent=False` | 回原 Worker 使用同一实例；丢失报 `execution`；只析构原实例。 |
| 可恢复非持久 | 成对提供 locator、restore，`persistent=False` | 缓存未命中可恢复等价访问能力；清理只处理已有副本，不为析构新建连接。 |
| 可恢复持久 | `persistent=True`，提供非 `None` 的 locator、restore 和 dispose | 恢复同一外部对象或精确版本；必要时跨 Worker 恢复后幂等删除。 |

钩子支持同步返回或 awaitable。`restore` 必须为类方法或静态方法，返回精确的声明类。未直接装饰的子类不是资源类。定位符经无损 JSON codec 深拷贝，可能包含大整数或 `Decimal`；调用 SDK 前由业务适配。

持久资源实例只保存定位信息或借用 Worker 管理的客户端。每次操作自行关闭临时连接、流和文件。事务提交、回滚等业务决策由实例方法实现；框架在最后引用消失后仍调用 `dispose`，资源类应记录是否已经完成，并容忍重复析构。

持久资源的 `restore` 不重建外部实体。即使实体已不存在，也应能返回保存原定位符的包装实例，由 `dispose` 幂等处理缺失；若 `restore` 因缺失而抛错，框架只能将删除记为未确认。例如，持久文件类的钩子可采用以下结构，其中 `storage` 是业务提供的按精确版本幂等删除接口：

```python
@classmethod
def restore(cls, locator):
    # 只恢复定位包装，不检查存在性，也不创建文件。
    return cls(key=locator["key"], version=locator["version"])

async def dispose(self):
    # 文件或该版本已不存在时同样成功。
    await storage.delete_if_exists(self.key, version=self.version)
```

## 所有权与回收

Activity 成功且完整输出通过校验后，新导出资源归当前 Run。未导出资源在 Attempt 本地回收；失败、取消或契约错误使全部新分配资源进入孤儿清理。输入资源仍由原所有者管理。

内联图和进程内调用共享 Registry。独立 Child 借入输入资源；成功返回的新资源先完整移交父 Run，再应用 `with.output`。未映射的新资源由父回收；Child 不销毁借入资源。

回收根包括实际执行环境、待提交完整结果及在途输入。覆写、分支抹除和退帧触发扫描。诊断快照和 trace 不持有资源；并发屏障中的结果保活到汇聚或异常排空。

框架保护存续，不安排业务访问顺序。同一资源可被多个调用借用；业务负责连接并发约束、幂等和迟到操作安全。Sticky 排队期限为 5 秒；全部输入可恢复时可回退共享队列，瞬态输入只能回同池原 Worker。含句柄接口的 Activity 使用 DSL 重试，关闭原生重试。

取消排空及本轮清理共用 30 秒上限，满足条件即开始。未实际排空的本地使用者继续保护实例；pending 条目拒绝新借用，排空后继续清理。析构失败保留原实例、标记和诊断，不替换业务异常。Worker 正常停机释放非持久缓存，仅摘除仍归 Run 所有的持久缓存。

业务及全部契约校验完成时确定本地结果；后续孤儿清理耗时不将及时成功改为本地超时。Server 的 Activity 硬超时仍使用原业务预算，不叠加清理时长；清理期间硬期限到达时，Workflow 仍观察到后端 `timeout`，不能据本地成功推定交接已被接受。

强制退出、未确认交接与清理失败仍可能留下外部对象。外部临时对象应使用独立代次或精确版本，并配置 TTL。超时不证明物理执行已停止，框架不承诺超时后的 exactly-once 副作用。

## 契约与部署

生成注解为 `Handle[C]`，精确资源类 C 只在 `TYPE_CHECKING` 分支导入。Worker 输入、输出与 `resources` 共用公共 Handle。

```sh
# --mount 决定 DSL 发现和契约文件位置；--module-mount 决定静态 Python 导入。
axcutor contracts workflows/billing --mount billing=workflows/billing --mount infra=workflows/infra \
  --module-mount billing=company.billing --module-mount infra=company.infrastructure
```

纯资源包也须通过 `--mount` 提供静态资源头文件。Worker 使用同一逻辑根的 Python 映射，运行期只加载 Python 包，不读取磁盘上的资源声明。静态检查环境须能导入精确资源类；映射不改变逻辑 kind 或契约 SHA。内部传输与清理状态见[生命周期规范](../../spec/05-executor-abi/05-resource-lifecycle.md)。
