# Docker 部署说明

> 前提：确保您已获取到所有配置，详见：[【DouYinSparkFlow 配置生成器】使用说明](配置生成器使用.md)

本项目支持通过 Docker 进行定时部署，适合部署在个人服务器、NAS 或支持 Docker 的运行环境中。

当前 Docker 方案已内置浏览器运行环境，并通过容器内 `cron` 实现每天定时执行。

## 1. 准备运行环境

部署设备需要提前安装以下工具：

1. `Docker`
2. `Docker Compose`（或支持 `docker compose` 子命令的 Docker 版本）

## 2. 拉取项目

首先拉取当前仓库到对应设备：`https://github.com/2061360308/DouYinSparkFlow.git`

## 3. 填写配置文件

Docker 部署时，程序会从容器内项目根目录的 `.env` 读取配置。

当前仓库已经在 `docker-compose.yml` 中预设了默认挂载规则，会将宿主机的 `./config/.env` 挂载为容器内 `/app/.env`。

操作步骤如下：

1. 在项目根目录下创建 `config` 目录。
2. 将根目录中的 `.env.example` 复制为 `config/.env`。
3. 打开已经填写好的配置生成器页面，点击左侧最下方 `复制 .env 配置文件` 按钮。
4. 将复制出的内容粘贴到 `config/.env` 中。
5. 检查并确认以下字段已经正确填写：`CRON_HOUR`、`CRON_MINUTE`、`CRON_SECOND`、`TZ`、`TASKS`、`COOKIES_<unique_id>`。

说明：

- `CRON_HOUR`、`CRON_MINUTE`、`CRON_SECOND` 用于控制每天执行时间。
- `TZ` 用于控制容器时区，默认推荐 `Asia/Shanghai`。
- `TASKS` 和 `COOKIES_<unique_id>` 是必填项。

## 4. 启动容器

项目根目录下执行以下命令：

```bash
docker compose up -d --build
```

首次启动会构建镜像并安装依赖，耗时会比后续启动更长一些。

## 5. 查看运行日志

容器标准输出日志可通过以下命令查看：

```bash
docker compose logs -f
```

此外，项目运行日志会默认持久化到宿主机的 `./logs` 目录，对应容器内路径为 `/app/logs`。

## 6. 修改挂载路径（可选）

如需自定义宿主机上的配置文件路径或日志目录，可在启动前指定以下环境变量：

1. `CONFIG_ENV_FILE`：宿主机上的 `.env` 文件路径
2. `LOGS_DIR`：宿主机上的日志目录路径

示例：

```bash
CONFIG_ENV_FILE=/data/douyin/config/.env LOGS_DIR=/data/douyin/logs docker compose up -d --build
```

说明：

- `CONFIG_ENV_FILE` 会被挂载为容器内 `/app/.env`
- `LOGS_DIR` 会被挂载为容器内 `/app/logs`

## 7. 常用命令

启动容器：

```bash
docker compose up -d
```

重新构建并启动：

```bash
docker compose up -d --build
```

停止容器：

```bash
docker compose down
```

查看容器状态：

```bash
docker compose ps
```

## 8. 注意事项

1. 容器内定时任务基于 `cron`，默认按 `TZ` 指定时区执行。
2. 修改 `config/.env` 后，建议重启容器使新的定时配置立即生效。
3. 如果只修改业务配置而不修改镜像内容，可直接执行 `docker compose restart`。
4. 若配置文件路径填写错误，容器启动时会直接报错退出。
5. 如需排查问题，建议先将日志级别设置为 `DEBUG`，再结合 `docker compose logs -f` 观察输出。
