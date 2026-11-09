在仓库根目录运行 `uv run --locked pyrefly check` 执行 Python 类型检查。开发依赖、CI、生成源码校验与文档统一使用 Pyrefly；检查继续覆盖运行时、宿主与 Playground 后端源码，以 Python 3.11 为目标，保留函数注解要求和未使用抑制检查。

builtin 与 module 生成契约继续提供输入字段、输出构造参数和分支返回类型检查，包括与 Pydantic 成员同名的字段及 `Branch.__dict__`。已有伴生契约通过 `axcutor contracts` 重新生成，使用 `--check` 核对部署前的源码一致性。
