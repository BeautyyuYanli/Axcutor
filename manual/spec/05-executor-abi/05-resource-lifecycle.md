# 05. 资源身份、授权与生命周期

本规范定义句柄内部传输与清理。业务接口见[资源句柄](../../docs/reference/05-resource-handles.md)。

## 身份与描述

公开信封是仅含两字段的闭合对象：

```json
{"$handle":"res_0123", "kind":"infra.postgres:Tx"}
```

两字段均为非空字符串。形状校验不授予访问权限。Python 冻结 Handle 按 rid 相等和哈希；传输值的比较复用递归 `literal_eq`。

内部描述含 rid、kind、locator、persistent、creator_queue 和 resident_queues。创建队列为 `<base_queue>-worker-<32 位小写 hex>`。身份及协议字段固定，仅驻留集合可以扩充。扫描只识别 TypeSpec 的 handle 位置，不扫描定位符或普通业务映射。

| 对象 | 权威范围 |
| --- | --- |
| Workflow Registry | Run 内 owned/borrowed 身份、调度授权、驻留及清理责任 |
| Worker Store | 真实实例、本地使用者和 pending 状态 |
| Attempt Context | 获准输入、本次分配及成功交接前的孤儿责任 |

## Activity 与 Child 交接

`resource_context.resources` 与按输入 schema 找到的 rid/kind 集合完全一致。未知 ID 不能凭自带 locator 注册。自定义 Executor 仍只返回 `{branch, output}`；全部 Activity 统一包装：

```text
{
  result: {branch, output},
  resources: {
    created: [新导出资源的规范描述],
    worker_queue: 当前驻留队列,
    resident_rids: [获准输入及新导出资源中仍驻留的 ID],
    default_queue: 原 Workflow 默认队列
  }
}
```

`default_queue` 从请求快照传至 Activity 并随结果记录。离线 SDK Replayer 使用自己的队列，后续路由检查使用记录中的原值。该字段不属于公开句柄或宿主接口。

分支选择、完整输出投影、Rust 契约和来源检查全部成功后才移交 created。输出 ID 只能来自获准输入或本次分配。未导出分配和失败分配本地清理，失败不移交新所有权。

Activity 在业务及全部契约校验完成边界检查本地期限并确定结果。后续孤儿清理使用独立的有界预算，不因清理耗时重新判定原结果；真实取消仍撤销未移交分配。Server 的 start-to-close 仍为原业务预算 A，不额外拼接清理时间；若硬期限先于完成响应到达，Workflow 仍按后端 timeout 处理。本地已确定成功不证明 Server 已接受交接。

ApplicationError details 保留选择器，携带驻留与业务排空状态。心跳不携带资源账本。取消或无法返回时，Worker 实际收尾后可向确切 Run 发送一次 resource_update，绑定调用 ID 和来源 Run。

独立 Child 使用相同外壳；residencies 累计多个 Worker 的驻留信息。输入为 borrowed，新分配为 owned。成功时完整移交输出中的新所有权，父接管后再投影；借入资源不改变所有权。普通失败只交回 borrowed 驻留。取消时可向直接父 Run 发送一次累计更新。

迟到更新只增加有效身份的驻留或清理责任，不重用业务输出，不重新激活已回收资源。根 Run 向宿主仍只返回原 `{branch, output}`。

## 根集合与提交

Registry 不存逐槽位引用计数。每次同步更新根，再扫描可达集合：

1. 实际执行帧环境；迭代诊断帧不持有资源。
2. 待提交完整结果，包括并发屏障内已完成分支。
3. 从派发到正常收尾或调度授权结束的调用输入。

节点提交、变量抹除、退帧与收尾触发扫描。子帧退出前保留完整结果，映射后同步替换根。并发失败先排空同组任务再移除结果；重试前丢弃失败尝试的待提交结果。扫描不 await，清理命令在扫描后发出。

owned 且不可达的资源进入 pending。borrowed 局部不可达只释放持有。诊断、trace、未确认使用与清理队列不是业务根。Run 退出清理剩余 owned，Child 先完成输出移交。缓存驱逐仅丢弃可重建状态，不发清理命令。

## Store 同步与回收

恢复、登记使用与清理共享逐 ID 同步边界。异步恢复后复核授权，禁止清理后重新入表；业务方法不在此锁内执行。Attempt 的使用者保留到业务及派生任务实际排空。

pending 拒绝新 get；已有使用者排空才 dispose。成功摘除条目，不保留永久墓碑；析构失败保留原实例与 pending，后续收尾或停机可再次尽力处理。

`dispose_resources` 返回逐项 `disposed / missing / unconfirmed` 及诊断。missing 只说明本 Store 无实例，不能覆盖此前失败，也不能确认持久外部对象已删除。非 persistent 不为清理恢复实例。persistent 未确认外部删除时可在创建池共享队列恢复并幂等删除；确认后仅摘除其余缓存。

## 路由、失联与预算

目标池由 executor 路由选项或 Workflow 默认队列决定。瞬态输入须全部来自目标池同一 Worker。全部可恢复时优先该池最近成功的 Sticky 队列，其次为输入描述有序的兼容驻留队列，最后为共享队列。

Sticky schedule-to-start 期限为 5 秒；未启动且输入全部可恢复时可回退。已启动调用超时或取消先请求取消并有界收尾，不能视为未领取透明重投。含句柄接口的 Activity 禁用原生重试，由 DSL 处理资源结果后决定重试。

排空及本轮清理共用 30 秒，各项消耗剩余时间；重复取消不续期。预算结束后 Workflow 结束旧调度授权，Worker 继续保护仍在实际使用的本地对象，并在排空后驱动已知清理。失联记录不无限保活资源。

复合 Worker 同时监听共享和独有队列，共用 Store、映射和 Activity。共享成员注册 Workflow 与两个 Activity，独有成员仅注册 execute_run、dispose_resources。停机先停止接收并排空 Activity，再关闭 Store；非持久缓存析构，仍归 Run 所有的持久缓存仅摘除本地引用。

框架不保证崩溃窗口绝对无泄漏或超时后的 exactly-once 副作用。业务负责精确版本、幂等、版本校验或 fencing；独立外部对象还需 TTL。
