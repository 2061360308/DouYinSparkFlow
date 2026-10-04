"""网页界面背后的业务层。

拆成三块，`Service` 是门面把它们拼起来，对外保持原有方法名：
    ConfigService   配置读写（.env ↔ Config）+ 账号档案挂载
    ScheduleService 运行模式与系统任务注册
    DesktopActions  打开目录 / URL
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.config import env_store, notify_spec, profile_store, settings
from app.config.models import (
    HITOKOTO_OPTIONS as HITOKOTO_OPTIONS_ALL,
    LOG_LEVEL_OPTIONS,
    TZ_OPTIONS,
    Account,
    Config,
    validate,
)
from app.errors import AppError
from app.paths import ENV_FILE
from app.util import open_in_system
from utils.logger import LOG_FILE as TASK_LOG

# 数值型配置的取值范围（界面渲染与保存时的 clamp 共用）
RANGES = {
    "browser_action_timeout": (5, 300),
    "im_scan_timeout": (10, 1800),
    "im_ready_timeout": (5, 300),
    "friend_list_wait_time": (1, 120),
    "im_max_steps": (10, 2000),
    "task_retry_times": (1, 5),
}


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
class ConfigService:
    def __init__(self, env_path=None) -> None:
        self.env_path = Path(env_path) if env_path else ENV_FILE
        self.config = Config()
        self.notes: list = []
        self.profile_index: dict = {}
        self.issues: list = []
        self.orphans: list = []
        self.reload()

    # -- 装载 ---------------------------------------------------------------
    def reload(self) -> None:
        self.config, self.notes = env_store.load_config(self.env_path)
        profile_index, _ = profile_store.load_with_notes()
        self.profile_index = profile_index
        self._attach_profile_folders()
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

    # -- 读 -> 页面 ---------------------------------------------------------
    def refresh_profiles(self) -> None:
        """重新读 profiles.json。

        会话列表是浏览器线程直接落盘的（profile_store.set_conversations），
        这里的内存缓存不会自动跟着变；不重读的话，拉完会话再 get_config
        拿到的还是旧名单，目标好友下拉就不会刷新。
        """
        self.profile_index = profile_store.load()

    def to_payload(self) -> dict:
        """页面启动/保存后调用的全量数据源（不含运行模式，那部分由门面补）。"""
        self.refresh_profiles()
        return {
            "config": self._config_to_dict(),
            "options": {
                "tz_options": list(TZ_OPTIONS),
                "hitokoto_options": list(HITOKOTO_OPTIONS_ALL),
                "log_level_options": list(LOG_LEVEL_OPTIONS),
                "ranges": {key: list(value) for key, value in RANGES.items()},
                "notify_types": [dict(item) for item in notify_spec.NOTIFY_TYPES],
            },
            "proxy": settings.proxy_config(),
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
            "notifications": [dict(item) for item in config.notifications],
            "accounts": [
                {
                    "username": account.username,
                    "unique_id": account.unique_id,
                    "cookies": account.cookies,
                    "targets": list(account.targets),
                    "profile_folder": account.profile_folder,
                    "fingerprint": account.fingerprint,
                    # 会话名单在 profiles.json（拉取会话列表的结果），
                    # 供前端渲染勾选目标好友的列表
                    "conversations": profile_store.conversations_for(
                        self.profile_index, account.unique_id
                    ),
                    "conversations_at": profile_store.conversations_at_for(
                        self.profile_index, account.unique_id
                    ),
                }
                for account in config.accounts
            ],
        }

    # -- 写 -> 页面 ---------------------------------------------------------
    def save(self, payload) -> dict:
        """页面把整份表单（config + proxy）交回来，就地写盘（不含调度联动）。"""
        if not isinstance(payload, dict):
            raise AppError("save_config 需要 config/proxy 对象")
        config = self._dict_to_config(payload.get("config") or {})

        notes, _orphans = env_store.save_config(config, self.env_path)
        try:
            settings.save_proxy(payload.get("proxy") or {})
        except Exception as exc:
            notes.append(f"本地设置保存失败：{type(exc).__name__}: {exc}")

        self.config = config
        self.notes = notes
        self._attach_profile_folders()
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
        notifications: list = []
        for raw in data.get("notifications") or []:
            if not isinstance(raw, dict):
                continue
            type_id = str(raw.get("type") or "").strip()
            spec = notify_spec.spec_for(type_id)
            if not spec:
                continue
            item = {"type": type_id, "enabled": bool(raw.get("enabled", True))}
            for field in spec.get("fields", []):
                key = field["key"]
                value = raw.get(key)
                if field.get("type") == "bool":
                    item[key] = bool(value)
                else:
                    item[key] = "" if value is None else str(value)
            notifications.append(item)
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
                *RANGES["browser_action_timeout"],
                self.config.browser_action_timeout,
            ),
            im_scan_timeout=clamp(
                data.get("im_scan_timeout"), *RANGES["im_scan_timeout"], self.config.im_scan_timeout
            ),
            im_ready_timeout=clamp(
                data.get("im_ready_timeout"), *RANGES["im_ready_timeout"], self.config.im_ready_timeout
            ),
            friend_list_wait_time=clamp(
                data.get("friend_list_wait_time"),
                *RANGES["friend_list_wait_time"],
                self.config.friend_list_wait_time,
            ),
            im_max_steps=clamp(
                data.get("im_max_steps"), *RANGES["im_max_steps"], self.config.im_max_steps
            ),
            task_retry_times=clamp(
                data.get("task_retry_times"),
                *RANGES["task_retry_times"],
                self.config.task_retry_times,
            ),
            log_level=str(data.get("log_level") or "Info") or "Info",
            notifications=notifications,
            accounts=accounts,
        )

    # -- 附加 ---------------------------------------------------------------
    def clean_orphans(self) -> dict:
        """删除 .env 里不再被任何账户引用的 COOKIES_*。"""
        removed = env_store.unset_keys(self.orphans, self.env_path)
        self.orphans = []
        self.reload()
        return {"removed": removed}

    def ping(self) -> dict:
        return {"pong": True, "time": datetime.now().strftime("%H:%M:%S")}


# ---------------------------------------------------------------------------
# 运行模式
# ---------------------------------------------------------------------------
class ScheduleService:
    def __init__(self, config_service: ConfigService) -> None:
        self._config = config_service

    def status(self) -> dict:
        """当前模式与系统任务注册状态；任何异常都退回「未注册」的中性结果。"""
        try:
            from app.scheduler import api

            return api.get_status()
        except Exception as exc:
            return {
                "mode": "",
                "mode_label": "",
                "installed": False,
                "available": False,
                "unavailable_reason": f"{type(exc).__name__}: {exc}",
                "modes": [],
                "mode_labels": {},
            }

    def set_mode(self, payload) -> dict:
        """切换运行模式：常驻定时 / 开机执行 / 生成配置。

        - 常驻定时、开机执行：本机跑，自动关闭隧道。
        - 生成配置：需要可用的隧道配置，校验通过才切换；不注册任务。
        注册失败或隧道校验失败会抛 AppError，前端据此保持原模式。
        """
        from app.scheduler import api

        payload = payload or {}
        mode = str(payload.get("mode") or "").strip()
        if mode not in api.MODES:
            raise AppError(f"未知模式：{mode!r}")

        if mode == api.MODE_CONFIG:
            proxy = payload.get("proxy") or settings.proxy_config()
            ok, why = settings.proxy_ready(proxy)
            if not ok:
                raise AppError("「生成配置」模式需要可用的隧道：" + why)
            settings.save_proxy(proxy)
            status = api.set_mode(api.MODE_CONFIG)
        else:
            # 本机执行：抓 Cookie 与跑任务同一出口，不需要隧道
            proxy = settings.proxy_config()
            if proxy.get("enabled"):
                proxy["enabled"] = False
                settings.save_proxy(proxy)
            status = api.set_mode(mode, run_time=self._config.config.run_time)

        return {"ok": True, "schedule": status, "proxy": settings.proxy_config()}

    def cancel(self) -> dict:
        """移除系统注册（任务 + 启动脚本），但保留当前模式，便于「重新注册」。"""
        from app.scheduler import api

        return api.cancel()

    def resync_scheduled(self, run_time: str) -> list:
        """保存配置后：若当前是「常驻定时」，按新的执行时间重新注册。返回提示列表。"""
        notes: list = []
        try:
            from app.scheduler import api

            status = api.get_status()
            if status.get("mode") == api.MODE_SCHEDULED and status.get("backend") != "noop":
                api.set_mode(api.MODE_SCHEDULED, run_time=run_time)
        except Exception as exc:
            notes.append(f"定时任务更新失败：{type(exc).__name__}: {exc}")
        return notes


# ---------------------------------------------------------------------------
# 打开目录 / URL
# ---------------------------------------------------------------------------
class DesktopActions:
    def open_env_dir(self, env_path) -> dict:
        target = Path(env_path).parent
        target.mkdir(parents=True, exist_ok=True)
        return {"path": open_in_system(target)}

    def open_external_url(self, payload) -> dict:
        url = str((payload or {}).get("url") or "").strip()
        if not url:
            raise AppError("open_external_url 需要 url")
        import webbrowser

        try:
            opened = webbrowser.open(url)
        except Exception as exc:
            raise AppError(f"打开浏览器失败：{type(exc).__name__}: {exc}") from exc
        return {"ok": bool(opened), "url": url}


# ---------------------------------------------------------------------------
# 执行日志
# ---------------------------------------------------------------------------
def _read_day_log(day: str) -> str:
    """从任务日志（app.log 及其轮转备份）里筛出该日期的行。"""
    base = Path(TASK_LOG)
    candidates = [base] + [base.with_name(f"{base.name}.{i}") for i in range(1, 4)]
    lines: list = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith(day):
                        lines.append(line.rstrip("\n"))
        except OSError:
            continue
    return "\n".join(lines)


class LogService:
    """执行日志：按天历史（热力图） + 某天的任务日志。"""

    def history(self, _payload=None) -> dict:
        try:
            from app.scheduler import api

            return {"days": api.history()}
        except Exception as exc:
            return {"days": {}, "error": f"{type(exc).__name__}: {exc}"}

    def day_log(self, payload) -> dict:
        day = str((payload or {}).get("date") or "").strip()
        if not day:
            raise AppError("schedule_day_log 需要 date")
        return {"date": day, "text": _read_day_log(day)}

    def open_log_dir(self, _payload=None) -> dict:
        target = Path(TASK_LOG).parent
        target.mkdir(parents=True, exist_ok=True)
        return {"path": open_in_system(target)}


# ---------------------------------------------------------------------------
# 门面
# ---------------------------------------------------------------------------
class Service:
    """把上面三块拼起来，对外保持原有方法名（host / 测试都用它）。"""

    def __init__(self, env_path=None) -> None:
        self.config_service = ConfigService(env_path)
        self.schedule = ScheduleService(self.config_service)
        self.desktop = DesktopActions()
        self.logs = LogService()
        self.env_path = self.config_service.env_path

    @property
    def config(self) -> Config:
        return self.config_service.config

    def reload(self) -> None:
        self.config_service.reload()

    def get_config(self, _payload=None) -> dict:
        data = self.config_service.to_payload()
        data["schedule"] = self.schedule.status()
        return data

    def save_config(self, payload) -> dict:
        result = self.config_service.save(payload)
        result["notes"].extend(
            self.schedule.resync_scheduled(self.config_service.config.run_time)
        )
        return result

    def clean_orphans(self, _payload=None) -> dict:
        return self.config_service.clean_orphans()

    def open_env_dir(self, _payload=None) -> dict:
        return self.desktop.open_env_dir(self.env_path)

    def open_external_url(self, payload) -> dict:
        return self.desktop.open_external_url(payload)

    def notify_test(self, payload) -> dict:
        """用页面当前填的参数发一条测试通知（不落盘）。"""
        from core import notify as notify_mod

        item = (payload or {}).get("notification") or {}
        ok, message = notify_mod.send_one(
            item, "这是一条测试通知：DouYinSparkFlow 通知配置成功。"
        )
        if not ok:
            raise AppError(message)
        return {"ok": True}

    def schedule_status(self, _payload=None) -> dict:
        return self.schedule.status()

    def schedule_set_mode(self, payload) -> dict:
        return self.schedule.set_mode(payload)

    def schedule_cancel(self, _payload=None) -> dict:
        return self.schedule.cancel()

    def schedule_history(self, _payload=None) -> dict:
        return self.logs.history()

    def schedule_day_log(self, payload) -> dict:
        return self.logs.day_log(payload)

    def open_log_dir(self, _payload=None) -> dict:
        return self.logs.open_log_dir()

    def ping(self, _payload=None) -> dict:
        return self.config_service.ping()


def make_bridge(bridge, worker_factory=None) -> "AccountOperator":
    """把 Service 与浏览器账号操作注册进桥。返回 AccountOperator（宿主主循环要 pump 它）。"""
    from app.browser.sessions import AccountOperator

    service = Service()
    ops = AccountOperator(bridge, worker_factory=worker_factory)
    bridge.register_all(
        ping=service.ping,
        get_config=service.get_config,
        save_config=service.save_config,
        clean_orphans=service.clean_orphans,
        open_env_dir=service.open_env_dir,
        open_external_url=service.open_external_url,
        notify_test=service.notify_test,
        schedule_status=service.schedule_status,
        schedule_set_mode=service.schedule_set_mode,
        schedule_cancel=service.schedule_cancel,
        schedule_history=service.schedule_history,
        schedule_day_log=service.schedule_day_log,
        open_log_dir=service.open_log_dir,
        account_login_start=ops.login_start,
        account_conversations_start=ops.conversations_start,
        account_open_browser=ops.open_browser,
        account_probe=ops.probe,
        account_grab=ops.grab,
        account_shutdown=ops.shutdown,
    )
    return ops
