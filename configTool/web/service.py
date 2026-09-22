"""网页界面背后的业务层：复用现有模块，不碰界面代码。

数据流：页面改动 → save_config(payload) → env_store 就地写 .env；
      载入/保存后 get_config() 返回最新状态（单一事实源在 Python 侧）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from configTool import env_store, local_settings, profile_store
from configTool.models import (
    HITOKOTO_OPTIONS as HITOKOTO_OPTIONS_ALL,
    LOG_LEVEL_OPTIONS,
    TZ_OPTIONS,
    Account,
    Config,
    validate,
)
from configTool.paths import ENV_FILE


class Service:
    def __init__(self, env_path=None) -> None:
        self.env_path = Path(env_path) if env_path else ENV_FILE
        self.config = Config()
        self.notes: list = []
        self.profile_index: dict = {}
        self.issues: list = []
        self.orphans: list = []
        self.reload()

    # ------------------------------------------------------------------ 装载
    def reload(self) -> None:
        self.config, self.notes = env_store.load_config(self.env_path)
        profile_index, _ = profile_store.load_with_notes()
        self.profile_index = profile_index
        self._attach_profile_folders()
        # reload 后配置对象可能换了，指纹/配置目录重新挂载过，以最新为准
        self.issues = validate(self.config)
        self.orphans = env_store.orphan_cookie_keys(self.config, self.env_path)

    def _attach_profile_folders(self) -> None:
        """把 .env 里的账号与 profiles.json 里的配置目录、指纹对应起来。

        指纹的权威来源是 profiles.json，找不到才回退用 .env 里的值；
        缺失指纹就现在分配并写回（指纹必须固定，拖到打开浏览器时才分配会漂移）。
        """
        for account in self.config.accounts:
            unique_id = account.unique_id.strip()
            from_env = account.fingerprint.strip()
            account.profile_folder = (
                profile_store.folder_for(self.profile_index, unique_id) if unique_id else ""
            )
            if account.profile_folder:
                account.fingerprint = profile_store.ensure_fingerprint(
                    unique_id, account.profile_folder, fallback=from_env
                )
        if self.config.accounts:
            self.profile_index = profile_store.load()

    # ------------------------------------------------------------ 读 -> 页面
    def get_config(self, _payload=None) -> dict:
        """页面启动/保存后调用的全量数据源。"""
        return {
            "config": self._config_to_dict(),
            "options": {
                "tz_options": list(TZ_OPTIONS),
                "hitokoto_options": list(HITOKOTO_OPTIONS_ALL),
                "log_level_options": list(LOG_LEVEL_OPTIONS),
                "ranges": {
                    "browser_action_timeout": [5, 300],
                    "im_scan_timeout": [10, 1800],
                    "im_ready_timeout": [5, 300],
                    "friend_list_wait_time": [1, 120],
                    "im_max_steps": [10, 2000],
                    "task_retry_times": [1, 5],
                },
            },
            "proxy": local_settings.proxy_config(),
            "notes": list(self.notes),
            "issues": [list(item) for item in self.issues],
            "orphans": list(self.orphans),
            "env_map": self.config.to_env_map(),
            "env_path": str(self.env_path),
        }

    def _config_to_dict(self) -> dict:
        config = self.config
        return {
            "proxy_address": config.proxy_address,
            "run_time": config.run_time,
            "tz": config.tz,
            "message_template": config.message_template,
            "hitokoto_types": list(config.hitokoto_types),
            "browser_action_timeout": int(config.browser_action_timeout),
            "im_scan_timeout": int(config.im_scan_timeout),
            "im_ready_timeout": int(config.im_ready_timeout),
            "friend_list_wait_time": int(config.friend_list_wait_time),
            "im_max_steps": int(config.im_max_steps),
            "task_retry_times": int(config.task_retry_times),
            "log_level": config.log_level or "Info",
            "accounts": [
                {
                    "username": account.username,
                    "unique_id": account.unique_id,
                    "cookies": account.cookies,
                    "targets": list(account.targets),
                    "profile_folder": account.profile_folder,
                    "fingerprint": account.fingerprint,
                }
                for account in config.accounts
            ],
        }

    # ------------------------------------------------------------ 写 -> 页面
    def save_config(self, payload) -> dict:
        """页面把整份表单（config + proxy）交回来，就地写盘。"""
        if not isinstance(payload, dict):
            raise ValueError("save_config 需要 config/proxy 对象")
        config_data = payload.get("config") or {}
        config = self._dict_to_config(config_data)

        notes, orphans = env_store.save_config(config, self.env_path)
        try:
            local_settings.save_proxy(payload.get("proxy") or {})
        except Exception as exc:
            notes.append(f"本地设置保存失败：{type(exc).__name__}: {exc}")

        self.config = config
        self.notes = notes
        self.issues = validate(config)
        self.orphans = env_store.orphan_cookie_keys(config, self.env_path)
        return {
            "notes": list(notes),
            "orphans": list(self.orphans),
            "issues": [list(item) for item in self.issues],
            "saved_at": datetime.now().strftime("%H:%M:%S"),
        }

    def _dict_to_config(self, data: dict) -> Config:
        def clamp(value, low, high, fallback):
            try:
                number = int(value)
            except (TypeError, ValueError):
                return fallback
            return max(low, min(high, number))

        accounts: list = []
        for raw in data.get("accounts") or []:
            raw = raw if isinstance(raw, dict) else {}
            targets = raw.get("targets") or []
            accounts.append(
                Account(
                    username=str(raw.get("username") or ""),
                    unique_id=str(raw.get("unique_id") or "").strip(),
                    cookies=str(raw.get("cookies") or ""),
                    targets=[str(t) for t in targets if str(t).strip()],
                    fingerprint=str(raw.get("fingerprint") or "").strip(),
                )
            )
        ranges = {
            "browser_action_timeout": (5, 300),
            "im_scan_timeout": (10, 1800),
            "im_ready_timeout": (5, 300),
            "friend_list_wait_time": (1, 120),
            "im_max_steps": (10, 2000),
            "task_retry_times": (1, 5),
        }
        return Config(
            proxy_address=str(data.get("proxy_address") or ""),
            run_time=str(data.get("run_time") or "09:00:00") or "09:00:00",
            tz=str(data.get("tz") or "Asia/Shanghai") or "Asia/Shanghai",
            message_template=str(data.get("message_template") or ""),
            hitokoto_types=[
                str(item)
                for item in (data.get("hitokoto_types") or [])
                if str(item).strip()
            ],
            browser_action_timeout=clamp(
                data.get("browser_action_timeout"),
                *ranges["browser_action_timeout"],
                self.config.browser_action_timeout,
            ),
            im_scan_timeout=clamp(
                data.get("im_scan_timeout"), *ranges["im_scan_timeout"], self.config.im_scan_timeout
            ),
            im_ready_timeout=clamp(
                data.get("im_ready_timeout"), *ranges["im_ready_timeout"], self.config.im_ready_timeout
            ),
            friend_list_wait_time=clamp(
                data.get("friend_list_wait_time"),
                *ranges["friend_list_wait_time"],
                self.config.friend_list_wait_time,
            ),
            im_max_steps=clamp(
                data.get("im_max_steps"), *ranges["im_max_steps"], self.config.im_max_steps
            ),
            task_retry_times=clamp(
                data.get("task_retry_times"),
                *ranges["task_retry_times"],
                self.config.task_retry_times,
            ),
            log_level=str(data.get("log_level") or "Info") or "Info",
            accounts=accounts,
        )

    # ------------------------------------------------------------ 附加操作
    def clean_orphans(self, _payload=None) -> dict:
        """删除 .env 里不再被任何账户引用的 COOKIES_*。"""
        removed = env_store.unset_keys(self.orphans, self.env_path)
        self.orphans = []
        self.reload()
        return {"removed": removed}

    def open_env_dir(self, _payload=None) -> dict:
        """在资源管理器中打开 .env 所在目录。"""
        return {"path": open_in_explorer(self.env_path.parent)}

    def ping(self, _payload=None) -> dict:
        return {"pong": True, "time": datetime.now().strftime("%H:%M:%S")}


def open_in_explorer(target) -> str:
    """跨平台打开目录，返回它的文本形式。"""
    import os
    import subprocess
    import sys

    try:
        target.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(target)  # noqa: S606 —— Windows 上即「在资源管理器中打开」
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return str(target)
    except Exception as exc:
        raise RuntimeError(f"打不开目录：{type(exc).__name__}: {exc}") from exc


def make_bridge(bridge) -> None:
    """把 Service 的方法注册进桥。"""
    service = Service()
    bridge.register_all(
        ping=service.ping,
        get_config=service.get_config,
        save_config=service.save_config,
        clean_orphans=service.clean_orphans,
        open_env_dir=service.open_env_dir,
    )