"""读取 .env 里的调度相关配置（不依赖 dotenv，独立可用）。

只关心两件事：
  - 每天执行的时刻（CRON_HOUR / CRON_MINUTE，秒忽略 —— cron 只到分钟）
  - 时区（仅作展示，实际「今天」用 OS 本地时区）
"""

from __future__ import annotations

from pathlib import Path

from app.scheduler import paths

DEFAULT_RUN_TIME = "09:00"


def read_env_file(path: Path | None = None) -> dict:
    """极简 .env 解析：KEY=VALUE，忽略注释与空行，去掉两侧引号。"""
    path = Path(path or paths.ENV_FILE)
    data: dict = {}
    if not path.is_file():
        return data
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            data[key] = value
    return data


def _clamp(raw, low: int, high: int, fallback: int) -> int:
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def resolve_run_time(env: dict | None = None) -> str:
    """返回 HH:MM。取 .env 的 CRON_HOUR/CRON_MINUTE，秒忽略。"""
    env = env if env is not None else read_env_file()
    default_h, default_m = DEFAULT_RUN_TIME.split(":")
    hour = _clamp(env.get("CRON_HOUR", default_h), 0, 23, int(default_h))
    minute = _clamp(env.get("CRON_MINUTE", default_m), 0, 59, int(default_m))
    return f"{hour:02d}:{minute:02d}"


def resolve_tz(env: dict | None = None) -> str:
    env = env if env is not None else read_env_file()
    return str(env.get("TZ") or "").strip() or "Asia/Shanghai"
