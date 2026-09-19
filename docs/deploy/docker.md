# 服务器 Docker 部署（推荐）

在个人服务器、NAS 或任何支持 Docker 的设备上部署。出口 IP 固定，登录态最稳。

## 1. 准备

1. 安装 Docker 和 Docker Compose
2. 用 configTool 生成 `.env`（见 [配置生成器](guide/03-配置生成器.md)）。不熟悉配置项的话可先参考仓库里的 [`.env.example`](https://github.com/2061360308/DouYinSparkFlow/blob/main/.env.example)

## 2. 两个容器

项目用 [`docker-compose.yml`](https://github.com/2061360308/DouYinSparkFlow/blob/main/docker-compose.yml) 一次启动两个容器：

| 容器 | 作用 |
| --- | --- |
| `douyin-spark-flow` | 任务执行器，容器内 cron 到点跑续火花 |
| `gost` | 配套代理，给本地 configTool 抓 Cookie 时借道 |

两个容器跑在同一台机器上，出口 IP **完全相同**，所以「抓 Cookie 的出口」和「跑任务的出口」天然一致，不需要额外的隧道配置。

## 3. 准备配置目录

```bash
mkdir -p ./config ./logs
```

把 configTool 生成的 `.env` 放到 `./config/.env`。

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

## 6. 给 configTool 配配套代理

本地 configTool 抓 Cookie 时，需要走服务器上的 gost 容器，让出口 IP 与任务一致。在 configTool 的「工具配置」页签填：

| 项 | 值 |
| --- | --- |
| 隧道地址 | `ws://<服务器公网IP>:9000?path=/ws` |
| 隧道账号 | 与 `GOST_USER` 一致 |
| 隧道密码 | 与 `GOST_PASSWORD` 一致 |

> 端口别省 —— configTool 不会给 `ws://` 补默认端口。

## 7. 安全提醒

- `.env` 里的 `GOST_PASSWORD` 要设成足够长的随机串（留空则 docker compose 无法启动）
- 建议在云服务器安全组里限制 9000 端口的来源 IP（只放你本机出口）
- 想要加密，把 gost 换成 `http+wss://` 并挂载证书，本地地址相应改成 `wss://`
