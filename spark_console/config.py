from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    cookie_key_file: Path
    session_key_file: Path
    timezone: str = "Asia/Shanghai"
    web_bind: str = "127.0.0.1"
    web_port: int = 8899
    worker_poll_seconds: int = 10
    clock_offset_limit_seconds: int = 5

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings":
        try:
            data_dir = Path(environ["SPARK_DATA_DIR"]).resolve()
            cookie_key_file = Path(environ["SPARK_COOKIE_KEY_FILE"]).resolve()
            session_key_file = Path(environ["SPARK_SESSION_KEY_FILE"]).resolve()
        except KeyError as error:
            raise ValueError(f"missing required setting: {error.args[0]}") from error

        data_dir.mkdir(parents=True, exist_ok=True)
        if len(cookie_key_file.read_bytes()) != 32:
            raise ValueError("cookie key must be exactly 32 bytes")
        if len(session_key_file.read_bytes()) < 32:
            raise ValueError("session key must contain at least 32 bytes")

        return cls(
            data_dir=data_dir,
            database_url=environ.get(
                "SPARK_DATABASE_URL", f"sqlite:///{data_dir / 'spark.db'}"
            ),
            cookie_key_file=cookie_key_file,
            session_key_file=session_key_file,
            timezone=environ.get("SPARK_TIMEZONE", "Asia/Shanghai"),
            web_bind=environ.get("SPARK_WEB_BIND", "127.0.0.1"),
            web_port=int(environ.get("SPARK_WEB_PORT", "8899")),
            worker_poll_seconds=int(environ.get("SPARK_WORKER_POLL_SECONDS", "10")),
            clock_offset_limit_seconds=int(
                environ.get("SPARK_CLOCK_OFFSET_LIMIT_SECONDS", "5")
            ),
        )
