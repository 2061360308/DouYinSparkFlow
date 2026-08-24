#!/bin/bash
set -euo pipefail

source /etc/douyin-spark-flow.env

echo "[docker] $(date '+%Y-%m-%d %H:%M:%S') start scheduled task"
if [[ -n "${CRON_SECOND:-}" && "${CRON_SECOND}" != "0" ]]; then
  sleep "${CRON_SECOND}"
fi
cd /app
python main.py
echo "[docker] $(date '+%Y-%m-%d %H:%M:%S') scheduled task finished"
