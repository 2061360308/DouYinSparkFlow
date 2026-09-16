"""拉取会话列表的独立窗口（全自动）。

流程 —— 用户不需要点任何按钮：

  1. 窗口一打开就用该账号**自己的浏览器配置目录**启动隐身浏览器，跳到抖音聊天页
  2. 等会话列表容器 ``.conversationConversationListwrapper`` 渲染出来
  3. 反复滚动这个容器，累计收集 ``.conversationConversationItemtitle`` 的文本，
     直到连续若干轮没有新会话 → 认定已到底
  4. 把名单交给主窗口（主窗口写进 profiles.json，并让用户直接点选目标好友）
     → 自动关窗、关浏览器

为什么默认 headless（不开窗口）：
    这一步纯粹是「读数据」，让浏览器窗口弹出来只会给用户误操作的机会
    （手滑点开某个会话、把列表滚走、甚至退出登录），都会让抓取结果不完整。
    所以默认无头运行，只在出问题时勾选「显示浏览器窗口」重试，方便看看到哪一步了。
"""

from __future__ import annotations

import queue
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

import profile_store
from browser_login import BrowserLoginWorker
from widgets import FONT_UI, ScrolledText

PUMP_INTERVAL_MS = 120
AUTO_CLOSE_DELAY_MS = 1500
# 工作线程自己也有超时（等容器 30s + 滚动 180s），这里再兜一层，防止界面永远转圈
HARD_TIMEOUT_S = 330.0

GREY = "#888780"
BLUE = "#185FA5"
GREEN = "#0F6E56"
RED = "#A32D2D"
AMBER = "#854F0B"


class ConversationDialog(tk.Toplevel):
    """用该账号的浏览器配置打开抖音，自动滚动收集全部会话名。"""

    def __init__(
        self,
        master,
        account,
        on_fetched=None,
        *,
        headless: bool = True,
        auto_close: bool = True,
        proxy=None,
    ) -> None:
        super().__init__(master)
        self.account = account
        self.on_fetched = on_fetched
        self.auto_close = auto_close
        # 云函数代理配置（local_settings.proxy_config()），由主窗口传入；
        # 与登录窗口走同一份配置，保证「登录」和「拉列表」是同一个出口
        self.proxy = dict(proxy or {})

        self.folder = str(getattr(account, "profile_folder", "") or "").strip()
        self.profile_dir = profile_store.profile_dir(self.folder)
        # 与「刷新登录信息」共用同一个固定指纹 —— 同一账号无论走哪条流程，
        # 在抖音眼里都应该是同一台设备
        self.fingerprint = profile_store.ensure_fingerprint(
            getattr(account, "unique_id", ""),
            self.folder,
            fallback=getattr(account, "fingerprint", ""),
        )

        # 所有 worker 共用同一个事件队列，重启时换线程即可（界面泵不用改）
        self.events: queue.Queue = queue.Queue()
        self.worker = None
        self.attempts = 0
        self.fetched: list = []

        self._closing = False
        self._done = False
        self._notified = False
        self._auto_close_job = None
        self._watchdog_job = None
        self._started = time.monotonic()
        # 默认无头（避免用户误操作）；勾上才把浏览器窗口显示出来，供排错
        self.show_var = tk.BooleanVar(value=not headless)

        self.title(f"拉取会话列表 · {account.display_name}")
        self.transient(master)
        self.resizable(True, True)
        self.minsize(620, 500)
        self._center_on(master)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(PUMP_INTERVAL_MS, self._pump)
        self.after(200, self._start)

        self._log(f"账号：{account.display_name}（抖音号 {account.unique_id or '未知'}）")
        self._log(f"浏览器配置目录：{self.profile_dir}")
        self._log(f"浏览器指纹：{self.fingerprint or '（随机）'} —— 固定值，每次都用同一个")
        self._log("将自动滚动会话列表，收集全部会话名 —— 不需要手动操作")

    # ------------------------------------------------------------------ 界面
    def _center_on(self, master) -> None:
        self.update_idletasks()
        width, height = 660, 540
        try:
            x = master.winfo_rootx() + (master.winfo_width() - width) // 2
            y = master.winfo_rooty() + (master.winfo_height() - height) // 3
        except Exception:
            x = (self.winfo_screenwidth() - width) // 2
            y = (self.winfo_screenheight() - height) // 3
        self.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text=(
                "会用这个账号自己的浏览器配置打开抖音，自动滚动会话列表并读取全部会话名。\n"
                "读完自动关闭，全程不需要手动操作。"
            ),
            font=FONT_UI,
            justify="left",
            foreground="#5F5E5A",
        ).pack(anchor="w")

        self.status_var = tk.StringVar(value="正在准备…")
        self.status_label = ttk.Label(
            outer,
            textvariable=self.status_var,
            font=("Microsoft YaHei UI", 10, "bold"),
            foreground=BLUE,
        )
        self.status_label.pack(anchor="w", pady=(10, 2))

        self.count_var = tk.StringVar(value="已抓到 0 个会话")
        ttk.Label(outer, textvariable=self.count_var, font=FONT_UI, foreground=GREY).pack(anchor="w")

        self.log_box = ScrolledText(outer, height=10, mono=True)
        self.log_box.container.pack(fill="both", expand=True, pady=(10, 0))

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(12, 0))
        self.btn_retry = ttk.Button(
            footer, text="重新打开浏览器", width=16, command=self._restart, state="disabled"
        )
        self.btn_retry.pack(side="left")
        ttk.Checkbutton(
            footer, text="显示浏览器窗口（排错用）", variable=self.show_var
        ).pack(side="left", padx=(10, 0))
        ttk.Button(footer, text="取消", width=10, command=self._on_close).pack(side="right")

    def _log(self, text: str) -> None:
        self.log_box.write_line(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    def _set_status(self, text: str, color: str = BLUE) -> None:
        self.status_var.set(text)
        self.status_label.configure(foreground=color)

    # ------------------------------------------------------------------ 启动
    def _start(self) -> None:
        if self._closing:
            return
        headless = not self.show_var.get()
        self.worker = BrowserLoginWorker(
            self.profile_dir,
            events=self.events,
            headless=headless,
            fingerprint=self.fingerprint,
            proxy=self.proxy,
        )
        self.worker.start()
        self._started = time.monotonic()
        self._set_status("正在启动浏览器…")
        self._log("浏览器以无头模式启动" if headless else "浏览器将以可见窗口启动")
        self.worker.send(
            "open",
            {
                "profile_dir": self.profile_dir,
                "ready_status": "浏览器已就绪 —— 正在加载会话列表",
            },
        )
        self._arm_watchdog()

    def _restart(self) -> None:
        """排错用：用当前勾选的状态重开一次（可切换显示窗口）。"""
        if self._closing:
            return
        self.btn_retry.config(state="disabled")
        self.attempts += 1
        self._log(f"重新打开浏览器（第 {self.attempts} 次重试）")
        if self.worker is not None:
            self.worker.send("shutdown")
        self.after(600, self._start)

    def _arm_watchdog(self) -> None:
        if self._watchdog_job is not None:
            try:
                self.after_cancel(self._watchdog_job)
            except Exception:
                pass
        self._watchdog_job = self.after(1000, self._watchdog)

    def _watchdog(self) -> None:
        self._watchdog_job = None
        if self._closing or self._done:
            return
        if time.monotonic() - self._started > HARD_TIMEOUT_S:
            self._fail(
                f"拉取超时（超过 {HARD_TIMEOUT_S:.0f} 秒）。\n\n"
                "可以勾选「显示浏览器窗口」后重试，看看卡在哪一步。",
                show_popup=False,
            )
            return
        self._watchdog_job = self.after(1000, self._watchdog)

    # ------------------------------------------------------------------ 事件泵
    def _pump(self) -> None:
        if self.worker is not None:
            try:
                while True:
                    kind, payload = self.events.get_nowait()
                    self._handle(kind, payload)
            except Exception:
                pass
        if not self._closing:
            self.after(PUMP_INTERVAL_MS, self._pump)

    def _handle(self, kind: str, payload) -> None:
        if kind == "log":
            self._log(str(payload))

        elif kind == "status":
            self._set_status(str(payload))

        elif kind == "opened":
            self._log("浏览器已打开，开始读取会话列表")
            self.btn_retry.config(state="normal")
            self._set_status("正在加载会话列表…")
            if self.worker is not None:
                self.worker.send("conversations")

        elif kind == "conversation_progress":
            info = payload or {}
            count = int(info.get("count") or 0)
            self.count_var.set(f"已抓到 {count} 个会话")
            if info.get("waiting"):
                self._set_status(f"已抓到 {count} 个，还在往下滚…")
            else:
                self._set_status(f"已抓到 {count} 个会话，继续滚动…")

        elif kind == "conversations":
            self._apply(payload or {})

        elif kind == "error":
            self._fail(str(payload))

    # ------------------------------------------------------------------ 结果
    def _apply(self, info: dict) -> None:
        if self._done or self._closing:
            return
        names = [str(name) for name in (info.get("names") or []) if str(name or "").strip()]
        stats = info.get("stats") or {}
        self.fetched = names

        self._log(
            f"滚动 {stats.get('rounds', '?')} 轮、耗时 {stats.get('elapsed', '?')} 秒，"
            f"共读到 {len(names)} 个会话"
            + ("（已到底）" if stats.get("hit_bottom") else "（可能还有更多）")
        )

        if not names:
            self._fail(
                "一个会话都没读到。\n\n"
                "常见原因：这个账号的登录态已失效，或者抖音改了页面结构。\n"
                "建议先点「刷新登录信息」重新登录，再回来拉取。",
                show_popup=False,
            )
            return

        for name in names[:8]:
            self._log(f"  · {name}")
        if len(names) > 8:
            self._log(f"  … 其余 {len(names) - 8} 个")

        self.count_var.set(f"已抓到 {len(names)} 个会话")
        self._done = True
        self._stop_watchdog()
        self._set_status(f"✔ 完成：读到 {len(names)} 个会话", GREEN)

        if self.on_fetched and not self._notified:
            self._notified = True
            try:
                self.on_fetched(self.account, list(names))
            except Exception as exc:
                self._log(f"回填到主窗口时出错：{type(exc).__name__}: {exc}")

        if self.auto_close and not self._auto_close_job:
            self._log("窗口即将自动关闭")
            self._auto_close_job = self.after(AUTO_CLOSE_DELAY_MS, self._shutdown)

    def _fail(self, message: str, *, show_popup: bool = True) -> None:
        self._log("失败：" + message.replace("\n\n", " ").replace("\n", " "))
        self._set_status("没抓成功，请看下方日志", RED)
        self.btn_retry.config(state="normal")
        if show_popup and not self._done:
            messagebox.showerror("拉取会话列表失败", message, parent=self)

    # ------------------------------------------------------------------ 关闭
    def _stop_watchdog(self) -> None:
        if self._watchdog_job is not None:
            try:
                self.after_cancel(self._watchdog_job)
            except Exception:
                pass
            self._watchdog_job = None

    def _on_close(self) -> None:
        if self._closing:
            return
        if not self._done and self.worker is not None and self.worker.is_running():
            if not messagebox.askyesno(
                "关闭窗口",
                "会话列表还没读完，关闭会同时关掉浏览器。确定关闭吗？",
                parent=self,
            ):
                return
        self._shutdown()

    def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop_watchdog()
        self.btn_retry.config(state="disabled")
        if self.worker is not None:
            self.worker.send("shutdown")
        self._wait_and_destroy(0)

    def _wait_and_destroy(self, ticks: int) -> None:
        alive = self.worker is not None and self.worker.is_alive()
        if alive and ticks < 60:
            self.after(100, lambda: self._wait_and_destroy(ticks + 1))
        else:
            try:
                self.grab_release()
            except Exception:
                pass
            self.destroy()
