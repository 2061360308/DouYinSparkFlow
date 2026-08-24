# Docker 部署说明

> 前提：确保您已获取到所有配置，详见：[【DouYinSparkFlow 配置生成器】使用说明](配置生成器使用.md)

本项目支持通过 Docker 进行定时部署，适合部署在个人服务器、NAS 或支持 Docker 的运行环境中。

当前推荐直接拉取已发布的镜像，再通过 `docker compose` 挂载配置文件和日志目录运行。

## 1. 准备运行环境

部署设备需要提前安装以下工具：

1. `Docker`
2. `Docker Compose`（或支持 `docker compose` 子命令的 Docker 版本）

## 2. 拉取镜像

```bash
docker pull ghcr.io/2061360308/douyinsparkflow:latest
```

## 3. 准备配置目录

Docker 部署时，程序会从容器内 `/app/.env` 读取配置。

推荐在宿主机创建 `/etc/donyinsparkflow/config` 和 `/etc/donyinsparkflow/log` 两个目录，用于放置配置和日志。

操作步骤如下：

1. 在宿主机上创建 `/etc/donyinsparkflow/config` 目录。
2. 将宿主机上的 `.env.example` 复制为 `/etc/donyinsparkflow/config/.env`。
3. 打开已经填写好的配置生成器页面，点击左侧最下方 `复制 .env 配置文件` 按钮。
4. 将复制出的内容粘贴到 `config/.env` 中。
5. 检查并确认以下字段已经正确填写：`CRON_HOUR`、`CRON_MINUTE`、`CRON_SECOND`、`TZ`、`TASKS`、`COOKIES_<unique_id>`。

说明：

- `CRON_HOUR`、`CRON_MINUTE`、`CRON_SECOND` 用于控制每天执行时间。
- `TZ` 用于控制容器时区，默认推荐 `Asia/Shanghai`。
- `TASKS` 和 `COOKIES_<unique_id>` 是必填项。

## 4. 编写 compose

在宿主机上创建 `compose.yml`，内容如下：

```yaml
services:
  douyin-spark-flow:
    image: ghcr.io/2061360308/douyinsparkflow:latest
    container_name: douyin-spark-flow
    restart: unless-stopped
    shm_size: 1gb
    environment:
      GITHUB_ACTIONS: "true"
      PYTHONUNBUFFERED: "1"
    volumes:
      - /etc/donyinsparkflow/config/.env:/app/.env:ro
      - /etc/donyinsparkflow/log:/app/logs
```

如果你想改成自定义路径，把上面的宿主机路径替换成你的实际路径即可。

## 5. 启动容器

在 `compose.yml` 所在目录执行以下命令：

```bash
docker compose -f compose.yml up -d
```

## 6. 查看运行日志

容器标准输出日志可通过以下命令查看：

```bash
docker compose -f compose.yml logs -f
```

此外，项目运行日志会默认持久化到宿主机的 `/etc/donyinsparkflow/log` 目录，对应容器内路径为 `/app/logs`。

## 7. 修改挂载路径（可选）

如需自定义宿主机上的配置文件路径或日志目录，直接修改 `compose.yml` 里的挂载路径即可。

## 8. 常用命令

启动容器：

```bash
docker compose -f compose.yml up -d
```

停止容器：

```bash
docker compose -f compose.yml down
```

查看容器状态：

```bash
docker compose -f compose.yml ps
```

## 9. 注意事项

1. 容器内定时任务基于 `cron`，默认按 `TZ` 指定时区执行。
2. 修改 `config/.env` 后，建议重启容器使新的定时配置立即生效。
3. 如果只修改业务配置而不修改镜像内容，可直接执行 `docker compose -f compose.yml restart`。
4. 若配置文件路径填写错误，容器启动时会直接报错退出。
5. 如需排查问题，建议先将日志级别设置为 `DEBUG`，再结合 `docker compose logs -f` 观察输出。
