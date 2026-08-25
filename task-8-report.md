# Task 8 本地实施报告

日期：2026-08-25
范围：仅执行计划 Task 8 的 Step 1–6；未执行 Step 7–10，未 fetch、push、SSH 或部署。

## 变更

- 在 `compose.console.yml` 增加单实例 `spark-auth`，运行 `python -m spark_console.auth_worker`。
- `spark-auth` 沿用只读 common 配置和 `spark-private`，不发布端口、不挂载 Docker socket；限制为 768 MiB、1 CPU 和 256 MiB `tmpfs`。
- 更新环境示例，说明认证进程与任务 Worker 共用数据库轮询间隔；未加入凭证值。
- 更新 README 与运维手册：升级前备份 SQLite，只构建、重建 `spark-web` 和 `spark-auth`；回滚停止 `spark-auth`、切换到上一控制台提交并只重建 `spark-web`。
- 回滚明确保留数据库、新增表、版本 2 账号、备份和 `spark-data` 卷，不启动或重建 `spark-worker`，不操作 BPS。

## TDD 证据

1. RED：新增部署契约测试后运行 `tests.console.test_deployment_contract`，因缺少 `spark-auth` 服务块而以 `IndexError` 失败（5 个测试中 1 个错误，退出码 1）。
2. GREEN：加入最小 Compose 服务后重跑同一测试模块，5/5 通过（退出码 0）。

## 本地验证证据

- 部署契约：5/5 通过。
- 全量测试：`python -m unittest discover -s tests -v`，142/142 通过，耗时 17.794 秒，退出码 0。
- 编译检查：`python -m compileall -q spark_console core utils`，退出码 0。
- 补丁格式：`git diff --check`，退出码 0；仅出现 Git 的 LF/CRLF 工作区提示，无空白错误。

测试环境显式设置了 `GITHUB_ACTIONS=true`、`WZ_DATA=[]` 和临时 `SPARK_LOG_DIR`。使用主仓库已有的 `.venv` 解释器；工作树内没有单独的 `.venv`。

## 自检

- [x] 命令、端口、Docker socket、资源限制、只读 common 与私网契约均有测试覆盖。
- [x] Compose 文件不包含 BPS 服务、网络、卷或端口配置。
- [x] 文档命令不嵌入密码、Token、Cookie、密钥内容或邀请码明文。
- [x] 部署与回滚命令均使用显式服务名，未包含启动 `spark-worker` 的命令。
- [x] 未触碰计划 Step 7–10 的远端发布和生产操作。
