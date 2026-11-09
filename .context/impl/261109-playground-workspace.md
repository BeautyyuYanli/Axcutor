克隆仓库后，在根目录运行 `docker compose up`，打开 `http://localhost:8080`，即可在文件树中编辑 YAML、JSON 和 Python，选择入口与输入文件，运行 module 工作流并查看 CFG、变量和执行帧。

Playground 后端独立位于 `playground/backend/`，通过 `axcutor-playground` 启动，单向依赖 host/runtime/core；`ExecutionService` 与普通 CLI/API 接口保持不变。示例只读挂载并首次复制到持久化 workspace，网页编辑不回写仓库。生成契约由生成器更新，保存通过 revision 防止静默覆盖。

后端使用一个常驻 Worker，Run 前清理 workspace 自有模块与字节码缓存再导入。一次运行一个根工作流，图内并发保留。提交、运行和本地清理期间禁止编辑、新建、删除、保存和再次 Run，后端拒绝相应写入；文件浏览、观察和取消仍可用，终态且清理完成后恢复操作。

执行历史和结果由 Temporal 保存；Playground 不保存运行副本或恢复记录，后端重启不续跑旧演示。提交画布仅关联本次实际源码；提交结果不确定时按返回 ID 查询和取消，不重复提交。
