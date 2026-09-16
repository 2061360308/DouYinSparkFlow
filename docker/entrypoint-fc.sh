#!/bin/bash
set -euo pipefail

# 云函数（FC）形态的入口：只负责把一个 HTTP Server 拉起来，
# 具体「什么请求跑任务、什么请求只回 200」全部在 /app/fc_server.py 里。
# 背景：自定义镜像函数的所有请求都走 HTTP 打到容器端口，定时触发器也不例外。

cd /app

# 日志实时性：FC 靠 stdout 采集，缓冲着不吐出来就等于没日志
export PYTHONUNBUFFERED=1

# FC 上 .env 通常不存在（配置来自函数环境变量 / 镜像内置 / 挂载），
# 所以只告警不退出 —— 和 cron 模式不同，那边缺配置直接跑没意义。
if [[ ! -f /app/.env ]]; then
  echo "[fc] 警告: /app/.env 不存在，配置将只来自进程环境变量" >&2
fi

echo "[fc] 启动 HTTP Server，等待定时触发器事件"
exec python main.py fc
