"""浏览器账户操作：添加账号 / 刷新登录 / 拉取会话列表。

旧 tkinter 界面里这些动作由 LoginDialog / ConversationDialog 驱动（1.5 秒探一次、
登录成功自动抓取、会话扫描进度等）。它们被删除后，逻辑仍全部收在
app/browser/worker.py 的 BrowserLoginWorker 里 —— 它天然就是
「命令队列 + 事件队列」的消息驱动设计，正适合接到桥（bridge）上。

本模块不做任何「抖音页面怎么点怎么滚」，只做三件事：
  1. 启动/终止一个 BrowserLoginWorker 会话（每个账户各用自己的配置目录）
  2. 把 worker 的事件（log / status / probe / grabbed / conversations …）转发成
     bridge 事件 ``browser_event`` 供前端渲染，并把结果写进 profiles.json
  3. 把成功抓到的账号通过 ``saved`` 事件交给前端去保存 .env
     （.env 的权威更新在 service.save_config，前端在收到 saved 后合并并保存）

新增的桥方法：
    account_login_start({mode, unique_id?})   开始一次登录（添加 / 刷新）
    account_open_browser(session_id)          重新打开浏览器
    account_probe(session_id)                 廉价探测一次当前状态
    account_grab(session_id, {manual})        手动抓取登录信息
    account_conversations_start({unique_id})  拉取某个账号的会话列表（无头）
    account_shutdown(session_id)              关闭浏览器并结束会话

前端通过 ``on("browser_event", ...)`` 订阅，payload 形如::

    {"session": "<id>", "kind": "log"|"opened"|"probe"|"grabbed"|
                           "conversation_progress"|"conversations"|"saved"|
                           "error"|"done"|"closed", "data": {...}}

saved 事件的 data 有两种：
  - 登录成功：{"kind": "login", "account": {...}, "mode": "add"|"refresh",
     "session": "<id>"}  前端据此把账号合并进配置并保存 .env
  - 会话列表读完：{"kind": "conversations", "names": [...], "session": "<id>"}
    前端据此触发一次 get_config 刷新会话勾选列表
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from datetime import datetime
from typing import Callable

from app import events
from app.config import settings, profile_store
from app.browser.worker import (
    CONVERSATION_READY_TIMEOUT_SECONDS,
    CONVERSATION_SCAN_TIMEOUT_SECONDS,
    BrowserLoginWorker,
    cookies_to_json,
)

# 自动流程的节奏（与旧 LoginDialog 保持一致）
POLL_INTERVAL_MS = 1500
LOOKUP_TIMEOUT_S = 12.0
MAX_AUTO_ATTEMPTS = 3
AUTO_CLOSE_DELAY_MS = 1200
CONV_AUTO_CLOSE_DELAY_MS = 1500

# 拉会话的硬超时：门禁 + 扫描 + 启动余量（与旧 ConversationDialog 一致）
CONVERSATION_HARD_TIMEOUT_S = (
    CONVERSATION_READY_TIMEOUT_SECONDS + CONVERSATION_SCAN_TIMEOUT_SECONDS + 120.0
)

# worker 只发这些 kind，其余一律不透传
_WORKER_EVENTS = events.WORKER_KINDS

# grabbed 里的大块数据（storage_state / local_storage）对前端没用，
# 转发时剥掉，避免把一个 MB 级对象塞进页面
_GRAB_SKIP_KEYS = ("storage_state", "local_storage")


def _now_label() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _trim_grabbed(data: dict) -> dict:
    """把 grabbed 事件里前端不需要的大字段剥离，只留落盘需要的。"""
    return {key: value for key, value in data.items() if key not in _GRAB_SKIP_KEYS}


class BrowserSession:
    """一个账户的一次浏览器操作会话（登录 / 拉会话共用同一个 worker）。"""

    def __init__(
        self,
        *,
        session_id: str,
        mode: str,  # "login" | "conversations"
        worker: BrowserLoginWorker,
        profile_dir,
        folder: str,
        fingerprint: str,
        existing_unique_id: str = "",
    ) -> None:
        self.session_id = session_id
        self.mode = mode
        self.worker = worker
        self.profile_dir = profile_dir
        self.folder = folder
        self.fingerprint = fingerprint
        # 这次会话对应的是哪个账号（刷新 / 拉会话时是它；添加时为空）
        self.existing_unique_id = existing_unique_id.strip()

        # 自动登录状态机（从旧 LoginDialog 移植）
        self.logged_since: float | None = None
        self.last_login_state = ""
        self.grabbed = False
        self.attempts = 0
        self.auto: bool = True
        self.done = False
        self.closed = False
        self.fetched_names: list = []
        self.opened = False
        self.last_probe = 0.0


class AccountOperator:
    """管理所有浏览器会话，并把它们的事件推送到前端。"""

    def __init__(self, bridge, worker_factory: Callable | None = None) -> None:
        self.bridge = bridge
        self.sessions: dict[str, BrowserSession] = {}
        self.lock = threading.Lock()
        # 测试可注入假 worker；默认构造真 BrowserLoginWorker
        self.worker_factory = worker_factory or self._default_worker

    def _default_worker(self, **kwargs) -> BrowserLoginWorker:
        return BrowserLoginWorker(**kwargs)

    # ------------------------------------------------------------------ 桥入口
    def login_start(self, payload) -> dict:
        """开始一次登录流程（添加账号 / 刷新登录信息）。

        payload: {"mode": "add"|"refresh", "unique_id": "..."}
        add 模式下新建随机目录；refresh 复用已有目录，找不到目录就新建。
        返回会话信息，之后事件全走 browser_event。
        """
        payload = payload or {}
        mode = str(payload.get("mode") or "add")
        if mode not in ("add", "refresh"):
            raise ValueError(f"mode 必须是 add 或 refresh，收到 {mode!r}")
        unique_id = str(payload.get("unique_id") or "").strip()
        if mode == "refresh" and not unique_id:
            raise ValueError("refresh 模式需要 unique_id")
        return self._spawn_login(mode, unique_id)

    def conversations_start(self, payload) -> dict:
        """开始拉取某个账号的会话列表（无头浏览器）。

        payload: {"unique_id": "..."}
        """
        unique_id = str((payload or {}).get("unique_id") or "").strip()
        if not unique_id:
            raise ValueError("conversations_start 需要 unique_id")
        return self._spawn_conversations(unique_id)

    def open_browser(self, payload=None) -> dict:
        session_id = self._session_id(payload)
        return self._send(session_id, "open")

    def probe(self, payload=None) -> dict:
        session_id = self._session_id(payload)
        return self._send(session_id, "probe")

    def grab(self, payload=None) -> dict:
        """手动抓取。manual=True 时允许刷新页面（用户自己就在浏览器前面）。"""
        payload = payload or {}
        session_id = self._session_id(payload)
        manual = bool(payload.get("manual"))
        session = self.sessions.get(session_id)
        if session is None:
            return {"ok": False, "error": "会话不存在或已结束"}
        session.grabbed = True
        session.worker.send(
            "grab",
            {"allow_reload": manual, "deep_login": bool(session.existing_unique_id)},
        )
        return {"ok": True}

    def shutdown(self, payload=None) -> dict:
        return self._send(self._session_id(payload), "shutdown")

    def _session_id(self, payload) -> str:
        return str((payload or {}).get("session") or "").strip()

    def _send(self, session_id, command, payload=None):
        session = self.sessions.get(session_id)
        if session is None:
            return {"ok": False, "error": "会话不存在或已结束"}
        session.worker.send(command, payload)
        return {"ok": True}

    # ------------------------------------------------------------------ 起会话
    def _resolve_folder(self, unique_id: str) -> dict:
        """按抖音号找回已有目录/指纹；没有就新建（这时还不落盘）。"""
        accounts = profile_store.load()
        existing = ""
        if unique_id:
            existing = profile_store.folder_for(accounts, unique_id)
        folder = existing or profile_store.random_folder_name()
        fingerprint = profile_store.ensure_fingerprint(unique_id, folder, fallback="")
        return {
            "folder": folder,
            "fingerprint": fingerprint,
            "existed": bool(existing),
        }

    def _spawn_login(self, mode: str, unique_id: str) -> dict:
        profile = self._resolve_folder(unique_id)
        folder = profile["folder"]
        fingerprint = profile["fingerprint"]
        profile_dir = profile_store.profile_dir(folder)
        proxy = settings.proxy_config()

        worker = self.worker_factory(
            profile_dir=profile_dir, fingerprint=fingerprint, proxy=proxy
        )
        session = BrowserSession(
            session_id=uuid.uuid4().hex[:8],
            mode="login",
            worker=worker,
            profile_dir=profile_dir,
            folder=folder,
            fingerprint=fingerprint,
            existing_unique_id=unique_id if mode == "refresh" else "",
        )
        with self.lock:
            self.sessions[session.session_id] = session
        session.worker.start()
        session.worker.send(
            "open",
            {
                "profile_dir": str(profile_dir),
                "ready_status": "浏览器已打开 —— 登录成功后会自动抓取并保存",
            },
        )
        self._forward(
            session,
            "log",
            f"方式：{'添加账号' if mode == 'add' else '刷新登录信息'}，"
            "登录成功后自动抓取并保存，无需手动操作",
        )
        self._forward(session, "log", f"配置目录：{profile_dir}")
        self._forward(
            session,
            "log",
            f"浏览器指纹：{fingerprint or '（随机）'} —— 固定值，每次打开浏览器都用同一个",
        )
        self._forward(
            session,
            "log",
            ("新配置目录：" if not profile["existed"] else "复用已有配置目录：")
            + profile_store.describe(folder),
        )
        return {
            "ok": True,
            "session": session.session_id,
            "mode": mode,
            "folder": folder,
            "fingerprint": fingerprint or "",
            "profile_dir": str(profile_dir),
        }

    def _spawn_conversations(self, unique_id: str) -> dict:
        profile = self._resolve_folder(unique_id)
        folder = profile["folder"]
        if not profile["existed"]:
            raise ValueError(
                "这个账号还没有浏览器配置目录 —— 请先点「刷新登录信息」完成一次登录"
            )
        profile_dir = profile_store.profile_dir(folder)
        proxy = settings.proxy_config()

        worker = self.worker_factory(
            profile_dir=profile_dir,
            headless=True,
            fingerprint=profile["fingerprint"],
            proxy=proxy,
        )
        session = BrowserSession(
            session_id=uuid.uuid4().hex[:8],
            mode="conversations",
            worker=worker,
            profile_dir=profile_dir,
            folder=folder,
            fingerprint=profile["fingerprint"],
            existing_unique_id=unique_id,
        )
        with self.lock:
            self.sessions[session.session_id] = session
        session.worker.start()
        session.worker.send(
            "open",
            {
                "profile_dir": str(profile_dir),
                "ready_status": "浏览器已就绪 —— 正在加载会话列表",
            },
        )
        self._forward(session, "log", f"浏览器配置目录：{profile_dir}")
        self._forward(session, "log", "浏览器以无头模式启动 —— 读列表不需要弹窗")
        return {
            "ok": True,
            "session": session.session_id,
            "folder": folder,
            "profile_dir": str(profile_dir),
        }

    # ------------------------------------------------------------------ 事件泵
    def pump(self) -> None:
        """把每个 worker 已排队的本地事件转发成 bridge 事件。

        在宿主主循环里每轮调用一次（与 bridge.drain 并列）。
        """
        with self.lock:
            sessions = list(self.sessions.values())
        now = time.monotonic()
        for session in sessions:
            if session.closed:
                continue
            # 登录会话每 1.5s 探一次（复刻旧 LoginDialog 的轮询，驱动自动抓取判定）
            if (
                session.mode == "login"
                and session.opened
                and not session.done
                and session.auto
                and (now - session.last_probe) >= POLL_INTERVAL_MS / 1000
            ):
                session.last_probe = now
                session.worker.send("probe")
            try:
                while True:
                    kind, data = session.worker.events.get_nowait()
                    self._handle(session, kind, data)
            except queue.Empty:
                pass

    def _forward(self, session, kind: str, data) -> None:
        """往 bridge 推一个 browser_event。grabbed 事件剥掉大字段。"""
        if data is not None and kind == "grabbed" and isinstance(data, dict):
            data = _trim_grabbed(data)
        self.bridge.emit(
            events.BROWSER_EVENT,
            {"session": session.session_id, "kind": kind, "data": data},
        )

    def _handle(self, session: BrowserSession, kind: str, data) -> None:
        if kind not in _WORKER_EVENTS or session.done:
            return
        if kind == "opened":
            self._forward(session, kind, data)
            session.opened = True
            # 拉会话流程：浏览器就位后立刻开始滚动扫描
            if session.mode == "conversations":
                session.worker.send("conversations")
            return
        if kind in ("log", "status", "conversation_progress", "error"):
            self._forward(session, kind, data)
            return
        if kind == "probe":
            self._on_probe(session, data)
            return
        if kind == "grabbed":
            self._on_grabbed(session, data)
            return
        if kind == "conversations":
            self._on_conversations(session, data)
            return
        if kind == "done":
            self._close_session(session)

    # ------------------------------------------------------------ 登录状态机
    def _on_probe(self, session: BrowserSession, payload: dict) -> None:
        """复刻旧 LoginDialog._on_probe：决定要不要自动抓取。"""
        if session.done:
            return
        payload = payload or {}
        if not payload.get("running"):
            self._forward(session, "status", "浏览器已关闭")
            return

        detected = payload.get("detected") or {}
        # 自动流程的门禁只看 logged_in（本地有没有 sessionid）
        logged_in = bool(payload.get("logged_in"))
        verdict = payload.get("login_state") or ""
        changed = verdict != session.last_login_state
        session.last_login_state = verdict

        if verdict == "EXPIRED":
            session.logged_since = None
            if changed:
                self._forward(
                    session, "log", "服务端已经不认这个登录态 —— 请在浏览器里重新登录"
                )
            self._forward(session, "status", "登录已失效 —— 请在浏览器里重新登录")
            return

        if not logged_in:
            session.logged_since = None
            self._forward(
                session, "status", "等待登录 —— 请在浏览器里扫码或短信登录"
            )
            return

        if session.logged_since is None:
            session.logged_since = time.monotonic()
            self._forward(session, "log", "检测到登录态，正在读取账号信息…")
        self._forward(session, "status", "已检测到登录态 —— 正在读取账号信息…")

        if session.grabbed:
            return
        if not session.auto:
            self._forward(session, "status", "已检测到登录态 —— 点「立即抓取」保存")
            return

        unique_id = detected.get("unique_id") or ""
        waited = time.monotonic() - session.logged_since
        # 刷新登录会跑完整判定（deep_login=True）；添加账号只按 Cookie 门禁
        deep_login = bool(session.existing_unique_id)
        if unique_id:
            self._forward(session, "status", "已识别账号信息 —— 正在抓取 Cookie…")
            session.grabbed = True
            session.worker.send("grab", {"allow_reload": False, "deep_login": deep_login})
        elif waited >= LOOKUP_TIMEOUT_S:
            self._forward(session, "log", "已登录但还没截到账号信息，抓取一次试试")
            session.grabbed = True
            session.worker.send("grab", {"allow_reload": False, "deep_login": deep_login})

    def _on_grabbed(self, session: BrowserSession, payload: dict) -> None:
        """复刻旧 LoginDialog._apply_grab：校验 + 不合格自动重试 + 成功落盘。"""
        if session.done:
            return
        payload = payload or {}
        cookies = payload.get("cookies") or []
        logged_in = bool(payload.get("logged_in"))
        detected = payload.get("detected") or {}
        login_state = payload.get("login_state") or ""
        nickname = str(detected.get("nickname") or payload.get("login_nickname") or "")
        unique_id = str(detected.get("unique_id") or "")

        self._forward(
            session,
            "log",
            f"共取得 {len(cookies)} 项 Cookie（原始 {payload.get('total', 0)} 项）",
        )
        self._forward(
            session,
            "log",
            "Cookie 登录态：" + ("有 sessionid" if logged_in else "没有 sessionid"),
        )
        if login_state:
            self._forward(
                session,
                "log",
                "服务端判定："
                + {"LOGGED_IN": "认可", "EXPIRED": "已失效"}.get(login_state, login_state),
            )
        if nickname or unique_id:
            self._forward(
                session,
                "log",
                f"自动识别到：昵称「{nickname}」抖音号「{unique_id}」"
                f"（来源 {detected.get('source', '')}）",
            )

        # 换账号保护：刷新某个账号却登进了另一个账号 → 不保存
        own = session.existing_unique_id
        if own and unique_id and unique_id.upper() != own.upper():
            self._forward(
                session,
                "log",
                f"登录的是「{unique_id}」，与当前账号「{own}」不一致 —— 本次不保存。"
                "要换账号请先移除这个账户，再重新添加。",
            )
            self._forward(
                session,
                "status",
                "登录的账号与当前账号不一致，本次不保存",
            )
            session.auto = False
            session.grabbed = False
            return

        # 校验：不合格就自动重试（这里依次通过就代表成功）
        if login_state == "EXPIRED":
            self._retry(
                session, "登录已失效", "本地还留着 sessionid，但服务端已经不认了，请重新登录"
            )
            return
        if not logged_in:
            self._retry(session, "未检测到登录态", "请在浏览器里确认已登录")
            return
        if not cookies:
            self._retry(session, "没拿到任何 Cookie", "稍后会自动重试")
            return
        if not unique_id:
            self._retry(
                session,
                "没能识别抖音号",
                "抖音号决定 .env 里的 COOKIES_ 键名，不能为空。请在浏览器窗口里按 F5 刷新一次",
            )
            return

        # 成功 → 落盘 profiles.json，账号数据交给前端去保存 .env
        folder = session.folder
        try:
            profile_store.bind(
                unique_id,
                folder,
                nickname=nickname,
                uid=str(detected.get("uid") or ""),
                sec_uid=str(detected.get("sec_uid") or ""),
                id_source=str(detected.get("id_source") or ""),
                fingerprint=session.fingerprint,
            )
            self._forward(
                session,
                "log",
                f"已写入 {profile_store.INDEX_FILE.name}：{unique_id} -> {folder}",
            )
        except Exception as exc:
            self._forward(
                session,
                "log",
                f"警告：profiles.json 写入失败（{type(exc).__name__}: {exc}）",
            )

        account = {
            "username": nickname,
            "unique_id": unique_id,
            "cookies": cookies_to_json(cookies, escaped=True),
            # targets 由前端在老账号基础上合并，这里只给空的
            "targets": [],
            "profile_folder": folder,
            "fingerprint": session.fingerprint,
            "conversations": [],
        }
        self._forward(
            session,
            "saved",
            {
                "kind": "login",
                "account": account,
                "mode": "refresh" if own else "add",
                "session": session.session_id,
            },
        )
        self._forward(
            session,
            "status",
            f"完成：{nickname or unique_id} · {len(cookies)} 项 Cookie，正在关闭浏览器…",
        )
        session.done = True
        # 稍后自动关浏览器（与旧对话框一致）
        threading.Timer(AUTO_CLOSE_DELAY_MS / 1000, self._close_session, (session,)).start()

    def _retry(self, session: BrowserSession, title: str, detail: str) -> None:
        session.attempts += 1
        self._forward(session, "log", f"{title}：{detail}")
        session.grabbed = False
        session.logged_since = None
        if session.attempts >= MAX_AUTO_ATTEMPTS:
            session.auto = False
            self._forward(
                session,
                "status",
                f"{title} —— 已停止自动重试，请点「立即抓取」",
            )
            self._forward(session, "log", "自动重试已到上限，改为手动")
            return
        self._forward(
            session,
            "status",
            f"{title}，稍后自动重试（{session.attempts}/{MAX_AUTO_ATTEMPTS}）",
        )

    # ------------------------------------------------------------ 会话列表
    def _on_conversations(self, session: BrowserSession, payload: dict) -> None:
        if session.done:
            return
        payload = payload or {}
        names = [str(n) for n in (payload.get("names") or []) if str(n or "").strip()]
        session.fetched_names = list(names)
        stats = payload.get("stats") or {}
        self._forward(session, "conversations", {"names": names, "stats": stats})
        self._forward(
            session,
            "log",
            f"滚动 {stats.get('rounds', '?')} 步、耗时 {stats.get('elapsed', '?')} 秒，"
            f"共读到 {len(names)} 个会话"
            + ("（已到底）" if stats.get("hit_bottom") else "（可能还有更多）"),
        )
        if not names:
            self._forward(
                session,
                "status",
                "没读到会话 —— 请先点「刷新登录信息」重新登录，再回来拉取",
            )
            return
        try:
            profile_store.set_conversations(
                session.existing_unique_id, names, folder=session.folder
            )
        except Exception as exc:
            self._forward(
                session,
                "log",
                f"会话名单写入失败（{type(exc).__name__}: {exc}）",
            )
        self._forward(
            session,
            "saved",
            {
                "kind": "conversations",
                "names": names,
                "session": session.session_id,
            },
        )
        self._forward(session, "status", f"完成：读到 {len(names)} 个会话，正在关闭浏览器…")
        session.done = True
        threading.Timer(
            CONV_AUTO_CLOSE_DELAY_MS / 1000, self._close_session, (session,)
        ).start()

    # ------------------------------------------------------------------ 收尾
    def _close_session(self, session: BrowserSession) -> None:
        """关闭浏览器、结束会话、通知前端。幂等。"""
        if session.closed:
            return
        session.closed = True
        try:
            session.worker.send("shutdown")
        except Exception:
            pass
        self._forward(session, "closed", {"session": session.session_id})
        with self.lock:
            self.sessions.pop(session.session_id, None)