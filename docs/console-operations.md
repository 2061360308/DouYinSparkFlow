# 火花控制台运维手册

## 安全边界

控制台默认只监听 `127.0.0.1:8899`。没有可信域名与 HTTPS 时只可通过 SSH 隧道维护，不得放通公网端口。部署与回滚不得连接、停止或清理 BPS 的容器、镜像、网络、卷和端口。

Worker 只有一个实例、并发为 1；失败任务不会自动重试。启用 Worker 前必须确认系统时间误差不超过 5 秒、根分区空闲不少于 5 GiB，并记录现有业务容器健康状态。

`spark-auth` 是独立的单实例扫码进程，只加入 `spark-private`，不发布宿主机端口、不挂载 Docker socket，并沿用只读根文件系统和 `no-new-privileges`。它与 `spark-web` 共享 `spark-data` 和两份只读密钥文件；不得把密钥内容写入 `.env.console`、命令行、聊天或日志。

## 首次安装

```bash
install -d -m 0750 /opt/douyin-spark-console/secrets
cp .env.console.example .env.console
docker compose --env-file .env.console -f compose.console.yml build spark-web spark-auth
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
docker compose --env-file .env.console -f compose.console.yml up -d --no-deps spark-web
curl --fail http://127.0.0.1:8899/health/ready
```

创建管理员时密码由隐藏提示读取，不出现在 shell 历史：

```bash
docker compose -f compose.console.yml run --rm spark-web python -m spark_console.cli create-admin admin
```

管理员可在网页创建普通用户；临时密码只显示一次，用户首次登录必须修改。

## 二维码与邀请功能升级

升级前记录当前控制台提交和容器状态，并执行 SQLite 备份。`backup-db` 输出卷内备份路径；必须保存该路径并确认命令成功后才能继续。备份文件、当前数据库和 `spark-data` 卷都不得删除。

```bash
git rev-parse HEAD
docker compose --env-file .env.console -f compose.console.yml ps
docker compose --env-file .env.console -f compose.console.yml run --rm spark-web python -m spark_console.cli backup-db
```

只构建 `spark-web` 与 `spark-auth`，然后只重建这两个服务。`--no-deps` 和显式服务名是部署边界：不要使用未列服务名的 `up`，不要启动或重建 `spark-worker`，也不要停用旧 timers。

```bash
docker compose --env-file .env.console -f compose.console.yml build spark-web spark-auth
docker compose --env-file .env.console -f compose.console.yml up -d --no-deps spark-web spark-auth
curl --fail https://wangze.oilu.cn/health/ready
docker compose --env-file .env.console -f compose.console.yml ps
```

验收时确认 `spark-auth` 为运行状态且 `PORTS` 为空，`spark-web` 仍只发布到回环地址；同时对照升级前记录，确认 BPS 容器、网络、卷、端口和旧 timers 都未改变。自动化检查不得生成或打印邀请码明文，也不得展示 Cookie、Token、存储状态、二维码历史或环境内容。

若验收失败，只停止 `spark-auth`，再切换到已审核的上一控制台提交，并只构建、重建 `spark-web`：

```bash
docker compose --env-file .env.console -f compose.console.yml stop spark-auth
git switch --detach <previous-console-commit>
docker compose --env-file .env.console -f compose.console.yml build spark-web
docker compose --env-file .env.console -f compose.console.yml up -d --no-deps spark-web
curl --fail https://wangze.oilu.cn/health/ready
docker compose --env-file .env.console -f compose.console.yml ps
```

此回滚不恢复数据库、不删除新增表、版本 2 账号、备份或 `spark-data` 卷，也不启动或重建 `spark-worker`。旧版 Web 不执行版本 2 账号，但数据会保留以便再次升级；原有 Cookie-only 账号和旧 timers 保持原状。严禁运行 `down -v`，严禁操作 BPS 资源。

## 域名与 HTTPS

生产环境保持 `SPARK_WEB_PUBLISH_IP=127.0.0.1` 和
`SPARK_SECURE_COOKIES=true`，由宿主机 Nginx 代理控制台。仓库中的
[`deploy/nginx/wangze.oilu.cn.conf`](../deploy/nginx/wangze.oilu.cn.conf)
是当前域名入口配置；部署后先运行 `nginx -t`，通过后再 reload：

```bash
sudo install -m 0644 deploy/nginx/wangze.oilu.cn.conf /etc/nginx/sites-available/wangze.oilu.cn
sudo ln -s /etc/nginx/sites-available/wangze.oilu.cn /etc/nginx/sites-enabled/wangze.oilu.cn
sudo nginx -t
sudo systemctl reload nginx
```

使用 Certbot 签发证书并启用 HTTP 到 HTTPS 跳转：

```bash
sudo certbot --nginx -d wangze.oilu.cn --redirect
sudo certbot renew --dry-run
```

完成后验证域名、证书和回环端口；公网不得再直接访问 8899：

```bash
curl --fail https://wangze.oilu.cn/health/ready
curl --fail http://127.0.0.1:8899/health/ready
```

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
docker compose --env-file .env.console -f compose.console.yml run --rm spark-web python -m spark_console.cli backup-db
docker compose --env-file .env.console -f compose.console.yml stop spark-worker
```

回滚时先停止新 Worker，再重新启用原有 timers，并核对下一次触发时间。不要删除新数据库、旧配置、Docker 卷或备份。数据库恢复必须在 Web 和 Worker 均停止时进行，并先保留当前数据库副本。

撤销访问人员使用的临时 SSH 公钥是每次远程维护完成后的必做步骤。
