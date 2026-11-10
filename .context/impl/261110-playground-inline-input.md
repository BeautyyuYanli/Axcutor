Playground 顶部直接编辑 Input JSON，支持紧凑展示和展开多行编辑；Entry 仅列出工作流定义。输入默认按入口契约的字段声明顺序生成递归骨架，空契约为 `{}`，内置 demo 默认使用 `{"items":[3,1,2],"delay_seconds":5}`。浏览器通过 WASM 即时检查 JSON 语法、重复键和字段类型，失败时显示诊断并禁用 Run。

Input 草稿以原始 JSON 文本按 `entryPath` 保存在当前页面内存中，切换入口保留已编辑内容。草稿未编辑时随编译契约更新默认值；修改 Input 不改变工作区 dirty 状态或 revision。运行输入不再依赖工作区文件，示例输入文件已移除。原始输入和 Schema 字面量保留任意精度数值与对象键序，提交、后端解析及运行时传输使用无损 JSON 契约。

`POST /v1/workspace/executions` 接收必填 `revision`、`entry`，以及可选对象 `input`；省略 input 时使用 `{}`。非对象输入返回 `400 invalid_request`，入口类型校验失败返回 `422 input_validation_failed`，版本不一致返回 `409 workspace_conflict`。成功返回 `202`，包含 execution、source 和 workspace。客户端以原始 Input JSON 构造请求体，调用方应直接提交对象内容，不再传输入文件路径。

提交响应及 `GET /v1/workspace/trace` 的 source 包含必填 `entry_path`、`entry`、`mounts`。`entry_path` 标识提交时的物理入口路径，`entry` 是编译与运行的逻辑标识；独立文件即使内容相同且都编译为 `__main__`，仍拥有各自草稿。页面刷新后，仅将最近执行的根调用输入恢复到路径相同且未编辑的草稿；恢复同时核对当前执行身份。`entry_path` 仅属于工作台元数据，编译请求保持 `{entry, mounts}`。

点击 Run 时仅在源码 dirty 时先保存工作区；源码未修改则直接使用当前 revision。Input 与源码在提交准备、运行及本地清理期间同步只读，清理完成后解除锁定。
