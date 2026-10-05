# Docker 部署

在个人服务器、NAS 或任何支持 Docker 的设备上部署。适用于能力强熟悉Docker的用户。

## 1. 准备

1. 安装 Docker 和 Docker Compose
2. 用 发行的app 生成 `.env`（见 [DouyinSparkFlow 发行包使用](deploy/release.md)）。

## 2. 两个容器

项目用 [`docker-compose.yml`](https://github.com/2061360308/DouYinSparkFlow/blob/main/docker-compose.yml) 一次启动两个容器：

| 容器 | 作用 |
| --- | --- |
| `douyin-spark-flow` | 任务执行器，容器内 cron 到点跑续火花 |
| `gost` | 配套代理，给本地 app 抓 Cookie 时借道 |

## 3. 准备配置目录

```bash
mkdir -p ./config ./logs
```

把 app 生成的 `.env` 放到 `./config/.env`。

## 4. 启动

```bash
docker compose up -d --build
```

## 5. 常用命令

```bash
docker compose logs -f      # 看日志
docker compose down         # 停止
docker compose restart      # 重启（改配置后）
```

## 6. 给 app 配配套代理

本地 app 抓 Cookie 时，需要走服务器上的 gost 容器，让出口 IP 与任务一致。在 app 的「工具配置」页签填：

| 项 | 值 |
| --- | --- |
| 隧道地址 | `ws://<服务器公网IP>:9000?path=/ws` |
| 隧道账号 | 与 `docker-compose.yml` 里 gost 命令的账号一致（默认 `gostuser`） |
| 隧道密码 | 与 `docker-compose.yml` 里 gost 命令的密码一致（默认 `gostpassword`） |

> 端口别省 —— app 不会给 `ws://` 补默认端口。

## 7. 安全提醒

- `docker-compose.yml` 里 gost 命令的密码要设成足够长的随机串
- 建议在云服务器安全组里限制 9000 端口的来源 IP（只放你本机出口）
- 想要加密，把 gost 换成 `http+wss://` 并挂载证书，本地地址相应改成 `wss://`
