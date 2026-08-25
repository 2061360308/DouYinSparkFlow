# 火花控制台运维手册

## 安全边界

控制台默认只监听 `127.0.0.1:8899`。没有可信域名与 HTTPS 时只可通过 SSH 隧道维护，不得放通公网端口。部署与回滚不得连接、停止或清理 BPS 的容器、镜像、网络、卷和端口。

Worker 只有一个实例、并发为 1；失败任务不会自动重试。启用 Worker 前必须确认系统时间误差不超过 5 秒、根分区空闲不少于 5 GiB，并记录现有业务容器健康状态。

## 首次安装

```bash
install -d -m 0750 /opt/douyin-spark-console/secrets
cp .env.console.example .env.console
docker compose -f compose.console.yml build
docker volume create douyin-spark-console_spark-data
docker run --rm -u 0 -v douyin-spark-console_spark-data:/data douyin-spark-console-spark-web chown -R 10001:10001 /data
```

密钥必须在服务器本机生成，不复制到命令行、聊天或日志：

```bash
umask 077
head -c 32 /dev/urandom > secrets/cookie.key
head -c 32 /dev/urandom > secrets/session.key
chown 10001:10001 secrets/*.key
chmod 0400 secrets/*.key
```

只启动 Web 并验证回环地址：

```bash
docker compose -f compose.console.yml up -d spark-web
curl --fail http://127.0.0.1:8899/health/ready
```

创建管理员时密码由隐藏提示读取，不出现在 shell 历史：

```bash
docker compose -f compose.console.yml run --rm spark-web python -m spark_console.cli create-admin admin
```

管理员可在网页创建普通用户；临时密码只显示一次，用户首次登录必须修改。

## 旧账号导入

旧 JSON 只从服务器文件读取，命令输出只包含账号和任务数量：

```bash
docker compose -f compose.console.yml run --rm spark-web python -m spark_console.cli import-legacy /private/path/usersData.json --owner admin
```

导入前保留原文件；导入成功不代表可以切换。需逐账号执行不发送消息的登录及目标精确匹配验证。旧 systemd timers 在明确批准切换前保持原状。

## 启用与观察

确认系统时间、导入验证和维护窗口均通过后，才可以停用旧 timers 并启动 Worker：

```bash
docker compose -f compose.console.yml up -d spark-worker
docker compose -f compose.console.yml ps
```

不要在日志中输出 Cookie、密码、环境变量或聊天正文。只报告任务 ID、阶段、成功/失败及脱敏错误码。

## 备份与回滚

```bash
docker compose -f compose.console.yml run --rm spark-web python -m spark_console.cli backup-db
docker compose -f compose.console.yml stop spark-worker
```

回滚时先停止新 Worker，再重新启用原有 timers，并核对下一次触发时间。不要删除新数据库、旧配置、Docker 卷或备份。数据库恢复必须在 Web 和 Worker 均停止时进行，并先保留当前数据库副本。

撤销访问人员使用的临时 SSH 公钥是每次远程维护完成后的必做步骤。
