"""浏览器登录窗口（全自动）。

流程 —— 用户不需要点任何按钮：

  1. 窗口一打开就启动隐身浏览器，跳到抖音聊天页
  2. 每 1.5 秒廉价探一次：登录了吗？账号信息接口截到了吗？
  3. 一旦登录成功 → 自动抓 Cookie + 昵称 + 抖音号 → 回填 → 写入 profiles.json
     → 通知主窗口保存 .env → 自动关窗

用户名和抖音号是**只读**的：它们来自抖音接口，手填只会填错，而抖音号直接决定
.env 里的 ``COOKIES_<抖音号>`` 键名 —— 填错了任务会被静默跳过。

浏览器配置目录按抖音号记录在 profiles.json（见 profile_store.py），
所以刷新登录信息能回到同一个目录，不必重新扫码。
"""

from __future__ import annotations

import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

import profile_store
from browser_login import CHAT_URL, BrowserLoginWorker, cookies_to_json
from widgets import FONT_MONO, FONT_UI, ScrolledText

# 自动流程的节奏
POLL_INTERVAL_MS = 1500
# 已登录但还没截到账号信息时，最多等这么久就强行抓一次（抓取时会刷新页面兜底）
LOOKUP_TIMEOUT_S = 12.0
# 自动重试上限，避免识别不到抖音号时无限循环抓取
MAX_AUTO_ATTEMPTS = 3
# 成功后停留多久再自动关窗（让用户看清结果）
AUTO_CLOSE_DELAY_MS = 1200

MODE_TITLES = {"add": "添加账号", "refresh": "刷新登录信息"}

GREY = "#888780"
BLUE = "#185FA5"
GREEN = "#0F6E56"
RED = "#A32D2D"
AMBER = "#854F0B"


class LoginDialog(tk.Toplevel):
    """登录并自动抓取账号信息的独立窗口。"""

    def __init__(
        self,
        master,
        account,
        on_saved=None,
        *,
        mode: str = "add",
        taken_ids=(),
        auto_close: bool = True,
        auto_grab: bool = True,
        proxy=None,
    ) -> None:
        super().__init__(master)
        self.account = account
        self.on_saved = on_saved
        # 云函数代理配置（local_settings.proxy_config()），由主窗口传入；
        # 启用后每次打开浏览器都会先拉起 gost 隧道，关窗即释放。
        self.proxy = dict(proxy or {})
        self.mode = mode if mode in MODE_TITLES else "add"
        self.auto_close = auto_close
        # add 模式下这些抖音号已经在列表里（用来提示「会更新而不是新增」）
        self.taken_ids = {
            str(item).strip().upper() for item in (taken_ids or ()) if str(item).strip()
        }

        # 配置目录：以前分配过就复用，否则分配一个随机的（此时还没落盘）
        index = profile_store.load()
        existing = str(getattr(account, "profile_folder", "") or "").strip() or (
            profile_store.folder_for(index, account.unique_id)
        )
        self.folder_is_new = not existing
        self.folder = existing or profile_store.random_folder_name()
        self.profile_dir = profile_store.profile_dir(self.folder)
        # 指纹跟着账号走：已有记录就沿用原来的，新账号分配一个（登录成功后随 bind 落盘）——
        # 保证同一个账号每次打开浏览器都是同一套指纹，而不是每次都换。
        # fallback 是 .env 的 TASKS 里带着的那份，profiles.json 查不到时用它。
        self.fingerprint = profile_store.ensure_fingerprint(
            account.unique_id, self.folder, fallback=getattr(account, "fingerprint", "")
        )

        self._closing = False
        self._done = False
        self._grabbed = False
        self._attempts = 0
        self._logged_since = None
        self._poll_job = None
        # 一次会话里只通知主窗口/只排一次自动关窗（万一用户又点了一次「立即抓取」，
        # 字段照常刷新，但不要重复写盘、重复倒计时）
        self._notified = False
        self._auto_close_job = None

        self.worker = BrowserLoginWorker(
            self.profile_dir, fingerprint=self.fingerprint, proxy=self.proxy
        )
        self.worker.start()

        self.title(f"{MODE_TITLES[self.mode]} · {account.display_name}")
        self.transient(master)
        self.resizable(True, True)
        self.minsize(620, 560)
        self._center_on(master)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._pump)
        self.auto_var.set(bool(auto_grab))
        self.after(200, self._auto_start)

        self._log(f"方式：{MODE_TITLES[self.mode]}（登录成功后自动抓取并保存，无需手动操作）")
        self._log(f"配置目录：{self.profile_dir}")
        self._log(
            f"浏览器指纹：{self.fingerprint or '（随机）'} —— 固定值，每次打开浏览器都用同一个"
        )
        if self.folder_is_new:
            if self.mode == "refresh" and account.unique_id:
                self._log(
                    f"注意：profiles.json 里没有「{account.unique_id}」的配置目录记录，"
                    "已分配一个新的 —— 需要在浏览器里重新登录一次"
                )
            else:
                self._log(f"新配置目录：{profile_store.describe(self.folder)}（随机命名，与抖音号无关）")
        else:
            self._log(f"复用已有配置目录：{profile_store.describe(self.folder)}")
        if account.has_cookies and self.mode == "refresh":
            self._log(f"该账号当前有 {account.cookie_count} 项 Cookie，刷新后会覆盖")

    # ------------------------------------------------------------------ 界面
    def _center_on(self, master) -> None:
        self.update_idletasks()
        width, height = 700, 600
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
                "窗口一打开就会自动启动浏览器 —— 在弹出的窗口里完成登录即可，\n"
                "登录成功后会自动抓取 Cookie、用户名、抖音号并保存，然后自动关掉本窗口。"
            ),
            font=FONT_UI,
            justify="left",
            foreground="#5F5E5A",
        ).pack(anchor="w")

        self.status_var = tk.StringVar(value="正在准备…")
        self.status_label = ttk.Label(
            outer, textvariable=self.status_var, font=("Microsoft YaHei UI", 10, "bold"), foreground=BLUE
        )
        self.status_label.pack(anchor="w", pady=(10, 10))

        fields = ttk.Frame(outer)
        fields.pack(fill="x")
        fields.columnconfigure(1, weight=1)
        fields.columnconfigure(3, weight=1)

        def readonly(row: int, column: int, label: str, var: tk.StringVar, mono: bool = False):
            ttk.Label(fields, text=label, font=FONT_UI).grid(
                row=row, column=column, sticky="w", padx=(0, 6), pady=3
            )
            entry = ttk.Entry(
                fields,
                textvariable=var,
                font=FONT_MONO if mono else FONT_UI,
                state="readonly",
            )
            entry.grid(row=row, column=column + 1, sticky="ew", padx=(0, 14), pady=3)
            return entry

        # 只读 —— 这些值全部来自抖音接口，不提供手工修改入口
        self.var_username = tk.StringVar(value=self.account.username)
        self.var_unique_id = tk.StringVar(value=self.account.unique_id)
        self.var_folder = tk.StringVar(value=profile_store.describe(self.folder))
        self.var_login = tk.StringVar(value="未连接")
        self.var_cookies = tk.StringVar(
            value=self.account.cookie_status if self.account.has_cookies else "等待抓取"
        )
        self.var_fingerprint = tk.StringVar(value=self.fingerprint or "（未分配）")

        self.entries = {
            "username": readonly(0, 0, "用户名", self.var_username),
            "unique_id": readonly(0, 2, "抖音号", self.var_unique_id),
            "folder": readonly(1, 0, "配置目录", self.var_folder, mono=True),
            "login": readonly(1, 2, "登录状态", self.var_login),
            "cookies": readonly(2, 0, "Cookies", self.var_cookies),
            "fingerprint": readonly(2, 2, "浏览器指纹", self.var_fingerprint, mono=True),
        }
        ttk.Label(
            fields,
            text="（以上均自动获取 / 分配，不可手工修改；指纹固定后每次打开浏览器都用同一个）",
            font=("Microsoft YaHei UI", 9),
            foreground=GREY,
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            outer,
            text="登录成功后自动抓取并保存（取消勾选则只在点『立即抓取』时抓）",
            variable=self.auto_var,
        ).pack(anchor="w", pady=(10, 6))

        self.log_box = ScrolledText(outer, height=9, mono=True)
        self.log_box.container.pack(fill="both", expand=True)

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(12, 0))
        self.btn_grab = ttk.Button(footer, text="立即抓取", width=12, command=self._on_manual_grab)
        self.btn_grab.pack(side="left")
        self.btn_open = ttk.Button(
            footer, text="重新打开浏览器", width=15, command=self._on_open, state="disabled"
        )
        self.btn_open.pack(side="left", padx=(8, 0))
        ttk.Button(footer, text="取消", width=10, command=self._on_close).pack(side="right")

    # -- 日志 ---------------------------------------------------------------
    def _log(self, text: str) -> None:
        self.log_box.write_line(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    def _set_status(self, text: str, color: str = BLUE) -> None:
        self.status_var.set(text)
        self.status_label.configure(foreground=color)

    # ------------------------------------------------------------------ 自动流程
    def _auto_start(self) -> None:
        if self._closing:
            return
        self.btn_open.config(state="disabled")
        self._set_status("正在启动隐身浏览器…")
        self.worker.send("open", self.profile_dir)

    def _on_open(self) -> None:
        self._auto_start()

    def _on_manual_grab(self) -> None:
        if self._closing:
            return
        self._grabbed = False
        self._attempts = 0
        self._grab()

    def _grab(self) -> None:
        if self._grabbed or self._closing:
            return
        self._grabbed = True
        self.btn_grab.config(state="disabled")
        self._set_status("正在读取登录信息…")
        self.worker.send("grab")

    # -- 轮询 ---------------------------------------------------------------
    def _start_poll(self) -> None:
        if self._poll_job is not None or self._closing:
            return
        self._poll_job = self.after(POLL_INTERVAL_MS, self._poll)

    def _stop_poll(self) -> None:
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None

    def _poll(self) -> None:
        self._poll_job = None
        if self._closing or self._done:
            return
        self.worker.send("probe")
        self._poll_job = self.after(POLL_INTERVAL_MS, self._poll)

    def _on_probe(self, payload: dict) -> None:
        if self._closing or self._done:
            return

        if not payload.get("running"):
            self._stop_poll()
            self.var_login.set("未连接")
            self._set_status("浏览器已关闭", AMBER)
            self.btn_open.config(state="normal")
            self.btn_grab.config(state="disabled")
            if not self._grabbed:
                self._log("浏览器窗口已关闭；点『重新打开浏览器』可以继续")
            return

        detected = payload.get("detected") or {}
        if detected.get("nickname"):
            self.var_username.set(detected["nickname"])
        if detected.get("unique_id"):
            self.var_unique_id.set(detected["unique_id"])

        if not payload.get("logged_in"):
            self._logged_since = None
            self.var_login.set("未登录")
            if self.auto_var.get():
                self._set_status("等待登录 —— 请在浏览器里扫码或短信登录")
            else:
                self._set_status("等待登录（自动抓取已关闭）", AMBER)
            return

        self.var_login.set("已登录 ✔")
        if self._logged_since is None:
            self._logged_since = time.monotonic()
            self._log("检测到登录态，正在读取账号信息…")

        if self._grabbed:
            return
        if not self.auto_var.get():
            self._set_status("已检测到登录态 —— 点『立即抓取』保存", AMBER)
            return

        waited = time.monotonic() - self._logged_since
        if detected.get("unique_id"):
            self._set_status("已识别账号信息，正在抓取 Cookie…")
            self._grab()
        elif waited >= LOOKUP_TIMEOUT_S:
            self._log(f"已登录但还没截到账号信息（等了 {waited:.0f} 秒），抓取一次试试")
            self._grab()

    # ------------------------------------------------------------------ 结果
    def _apply_grab(self, info: dict) -> None:
        cookies = info.get("cookies") or []
        logged_in = bool(info.get("logged_in"))
        detected = info.get("detected") or {}

        self._log(f"共取得 {len(cookies)} 项 Cookie（原始 {info.get('total', 0)} 项）")
        self._log("登录状态：" + ("已登录 ✔" if logged_in else "未检测到登录态 ✘"))

        nickname = str(detected.get("nickname") or "")
        unique_id = str(detected.get("unique_id") or "")

        if nickname or unique_id:
            self._log(
                f"自动识别到：昵称「{nickname}」抖音号「{unique_id}」"
                f"（来源 {detected.get('source', '')}）"
            )
            if detected.get("id_source") == "short_id（该账号未设自定义抖音号）":
                self._log("  注：该账号没设自定义抖音号，用的是 short_id（抖音界面展示的就是它）")
            if detected.get("uid"):
                self._log(f"  账号 uid：{detected['uid']}")
        else:
            self._log("没能自动识别昵称/抖音号")
            status = info.get("api_status") or {}
            if status.get("code") == 8:
                self._log(
                    "  原因：账号信息接口返回「用户未登录」，说明当前 Cookie 的登录态无效，"
                    "请在浏览器里重新登录"
                )
            elif status:
                self._log(f"  原因：账号信息接口未返回用户数据（status_code={status.get('code')}）")
            if info.get("capture_error"):
                self._log(f"  读取响应体时报错：{info['capture_error']}")

        if nickname:
            self.var_username.set(nickname)
        if unique_id:
            self.var_unique_id.set(unique_id)

        # -- 逐项校验，不合格就退回去继续等 -------------------------------
        if not logged_in:
            self._retry("未检测到登录态", "请在浏览器窗口里确认已登录抖音，登录成功后会自动重试")
            return
        if not cookies:
            self._retry("没拿到任何 Cookie", "稍后会自动重试；也可以点『立即抓取』手动重试")
            return
        if not unique_id:
            self._retry(
                "没能识别抖音号",
                "抖音号决定 .env 里的 COOKIES_ 键名，不能为空。\n"
                "请在浏览器里刷新一次页面（F5），会自动重试",
            )
            return

        # -- 换账号保护：刷新某个账号却登进了另一个账号 --------------------
        own = str(self.account.unique_id or "").strip()
        if self.mode == "refresh" and own and unique_id.upper() != own.upper():
            self._stop_poll()
            self._done = True
            self.auto_var.set(False)
            self.btn_grab.config(state="normal")
            self._log(
                f"登录的是「{unique_id}」，与当前账号「{own}」不一致 —— 本次不保存。"
            )
            self._log("要换账号请先移除这个账户，再重新添加。")
            self._set_status("登录的账号与当前账号不一致，本次不保存", RED)
            return

        # -- 落盘 -----------------------------------------------------------
        self.account.cookies = cookies_to_json(cookies, escaped=True)
        if nickname:
            self.account.username = nickname
        self.account.unique_id = unique_id
        self.account.profile_folder = self.folder
        self.account.fingerprint = self.fingerprint
        self.var_cookies.set(self.account.cookie_status)
        self.var_folder.set(profile_store.describe(self.folder))

        if unique_id.upper() in self.taken_ids and self.mode == "add":
            self._log(f"注意：「{unique_id}」已经在账户列表里，保存后会更新那一条而不是新增")

        saved = True
        try:
            profile_store.bind(
                unique_id,
                self.folder,
                nickname=nickname,
                uid=str(detected.get("uid") or ""),
                sec_uid=str(detected.get("sec_uid") or ""),
                id_source=str(detected.get("id_source") or ""),
                fingerprint=self.fingerprint,
            )
            self._log(
                f"已写入 {profile_store.INDEX_FILE.name}："
                f"{unique_id} -> {self.folder}（指纹 {self.fingerprint or '未指定'}）"
            )
        except Exception as exc:
            saved = False
            self._log(f"警告：profiles.json 写入失败（{type(exc).__name__}: {exc}）")
            self._log("  Cookie 仍会回填，但下次打开可能找不到这个配置目录")

        self._done = True
        self._stop_poll()
        self.btn_grab.config(state="normal")
        self.var_login.set("已登录 ✔")
        self._set_status(
            f"✔ 完成：{self.account.username or unique_id} · {len(cookies)} 项 Cookie",
            GREEN,
        )
        if not saved:
            self._set_status(f"✔ 完成，但配置目录记录失败 · {len(cookies)} 项 Cookie", AMBER)

        if self.on_saved and not self._notified:
            self._notified = True
            try:
                self.on_saved(self.account, self.mode)
            except Exception as exc:
                self._log(f"回填到主窗口时出错：{type(exc).__name__}: {exc}")

        if self.auto_close and not self._auto_close_job:
            self._log("窗口即将自动关闭")
            self._auto_close_job = self.after(AUTO_CLOSE_DELAY_MS, self._auto_close)

    def _retry(self, title: str, detail: str) -> None:
        """抓取结果不合格：回到等待状态，允许下一次自动重试。"""
        self._attempts += 1
        self._log(f"{title}：{detail.replace(chr(10), ' ')}")
        self._grabbed = False
        self._logged_since = time.monotonic()
        self.btn_grab.config(state="normal")
        if self._attempts >= MAX_AUTO_ATTEMPTS:
            self._stop_poll()
            self.auto_var.set(False)
            self._set_status(f"{title} —— 已停止自动重试，请点『立即抓取』", RED)
            self._log(f"自动重试已到 {MAX_AUTO_ATTEMPTS} 次上限，改为手动")
            return
        self._set_status(f"{title}，稍后自动重试（{self._attempts}/{MAX_AUTO_ATTEMPTS}）", AMBER)

    def _auto_close(self) -> None:
        if self._closing:
            return
        self._shutdown()

    # ------------------------------------------------------------------ 事件泵
    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.worker.events.get_nowait()
                self._handle(kind, payload)
        except Exception:
            pass
        if not self._closing:
            self.after(120, self._pump)

    def _handle(self, kind: str, payload) -> None:
        if kind == "log":
            self._log(payload)

        elif kind == "status":
            self._set_status(str(payload))

        elif kind == "opened":
            self.btn_open.config(state="normal")
            self.btn_grab.config(state="normal")
            self._start_poll()
            self._log("浏览器已打开 —— 完成登录后会自动抓取，不需要点任何按钮")
            self._set_status("等待登录 —— 请在浏览器里扫码或短信登录")

        elif kind == "probe":
            self._on_probe(payload or {})

        elif kind == "grabbed":
            self._apply_grab(payload or {})

        elif kind == "error":
            self._log(f"错误：{payload}")
            self._set_status("出错了，请看下方日志", RED)
            self.btn_open.config(state="normal")
            self.btn_grab.config(state="normal" if self.worker.is_running() else "disabled")
            if not self._done:
                messagebox.showerror("出错了", str(payload), parent=self)

    # ------------------------------------------------------------------ 关闭
    def _on_cancel_confirm(self) -> bool:
        if self._done:
            return True
        return messagebox.askyesno(
            "关闭登录窗口",
            "关闭会同时关掉浏览器窗口，未保存的登录信息会丢失。确定关闭吗？",
            parent=self,
        )

    def _on_close(self) -> None:
        if self._closing:
            return
        if self.worker.is_running() and not self._on_cancel_confirm():
            return
        self._shutdown()

    def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop_poll()
        self.btn_open.config(state="disabled")
        self.btn_grab.config(state="disabled")
        self.worker.send("shutdown")
        self._wait_and_destroy(0)

    def _wait_and_destroy(self, ticks: int) -> None:
        if self.worker.is_alive() and ticks < 60:
            self.after(100, lambda: self._wait_and_destroy(ticks + 1))
        else:
            try:
                self.grab_release()
            except Exception:
                pass
            self.destroy()
