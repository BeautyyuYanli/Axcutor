# Axcutor

Axcutor 将 YAML/JSON 工作流编译为自包含快照，通过 Temporal 执行强类型控制流图（CFG）和 Python Activity。

## 快速开始

1. **启动服务**

```sh
docker compose up
```

首次启动会自动构建镜像，并将 `examples/` 复制到宿主机 `playground/workspace/`（挂载到容器 `/workspace`，不纳入 Git 跟踪）。

2. **试跑内置工作流**：打开 [Playground](http://localhost:8080)，顶部默认已选中 **Entry** `demo.main`，**Input** 预填 `{"items":[3,1,2],"delay_seconds":5}`。直接点击 **Run**，在底部 **Execution** 与 **Trace** 面板观察子工作流并发倍增与菱形并发汇总（`total: 12, spread: 4`）。
3. **用 Agent 创建新工作流**：在仓库根目录向你的 Coding Agent 发送：

```text
阅读 manual/docs/guide，在 playground/workspace 下创建一个用辗转相除法求最大公约数的工作流包，并给出示例 Input JSON
```

4. **运行新工作流**：刷新 Playground 页面，在顶部 **Entry** 选择新工作流（如 `gcd.main`），直接编辑 **Input** JSON，必要时点击 **Expand Input** 展开。Input 根据入口契约生成默认骨架，校验通过后可点击 **Run**。各入口的已编辑草稿在当前页面内保留；Input 不写入工作区文件，单独修改 Input 不会保存源码。刷新页面后，仅文件路径与最近提交相同且 Input 未被编辑的入口可从根调用记录恢复输入；内容相同的其他文件保留各自的默认输入。

## 文档

`axcutor_host.AxcutorHost` 提供协议无关的执行管理；`axcutor` 命令由 `axcutor_cli` 提供，包含 `run`、`signal`、`contracts`、`worker`。执行通过 Temporal task queue 交给独立 Worker，不由宿主接口持有 Worker。

`axcutor-playground api` 启动独立执行 HTTP API（默认端口 `8000`），不创建工作区或 Worker；`axcutor-playground` 默认启动完整工作台（默认端口 `8080`），拥有工作区与驻留 Worker。部署参数与执行契约见 [Execution HTTP API](manual/docs/reference/04-execution-http-api.md)。

完整语言教程、实战配方与参考规范见 [文档中心](manual/index.md)（渐进式教程见 [manual/docs/guide](manual/docs/guide/index.md)）。
