"""DouYinSparkFlow 本地配置生成器（tkinter）。

把 docs/index.html 的功能搬到本地：左侧环境变量预览，右侧「基础配置 / 账户配置」，
所有改动自动写回程序目录下的 .env，启动时自动载入。

账户信息全部自动化：
  - 用户名 / 抖音号 / Cookies 都由浏览器登录后自动抓取，界面上只读（手填只会填错）
  - 每个账号用自己独立的浏览器配置目录，对照关系记在 profiles.json
  - 「刷新登录信息」用该账号原来的配置目录重开浏览器，不必重新扫码

目标好友：
  - 手填之外，多了一条「拉取会话列表」——用该账号自己的浏览器配置（无头）打开抖音，
    自动滚动会话列表容器、收全部会话名，存进 profiles.json，之后直接在界面里点选
  - 抓到的名单存在 profiles.json（本工具自己的元数据），**不写进 .env**

运行：
    cd configTool
    python main.py

本目录完全自包含（打包成 exe 后同样成立）：
  - .env、profiles/、profiles.json 都生成在本目录，跟着程序走
  - 只依赖同目录内的模块与第三方包，不引用仓库里其他目录

生成好的 .env 需要放到主程序的运行目录才会生效：
  本地运行 → 项目根目录（与 main.py 同级）；Docker → ./config/.env
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

import env_store
import profile_store
from conversation_dialog import ConversationDialog
from login_dialog import LoginDialog
from models import (
    BROWSER_TIMEOUT_RANGE,
    FRIEND_LIST_WAIT_RANGE,
    HITOKOTO_OPTIONS,
    LOG_LEVEL_OPTIONS,
    RETRY_TIMES_RANGE,
    TZ_OPTIONS,
    Account,
    Config,
    build_run_time,
    split_run_time,
    validate,
)
from widgets import FONT_MONO, FONT_UI, ConversationPicker, ScrolledText

AUTOSAVE_DELAY_MS = 1500


class ScrollFrame(ttk.Frame):
    """可滚动容器：把内容放进 .inner 即可。"""

    def __init__(self, master) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = ttk.Frame(self.canvas, padding=(18, 14))
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_inner(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event) -> None:
        # 指针停在自带滚动的控件（会话列表 Treeview、Cookies/日志文本框）上时，
        # 滚轮应该归它们，而不是把整个表单滚走。
        widget = self.winfo_containing(event.x_root, event.y_root)
        hops = 0
        while widget is not None and hops < 20:
            if isinstance(widget, (tk.Listbox, tk.Text, ttk.Treeview)):
                return
            if widget is self:
                break
            widget = getattr(widget, "master", None)
            hops += 1
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


def _wrap_to_width(label: ttk.Label, minimum: int = 220) -> None:
    """让一段说明文字跟着容器宽度自动换行。

    ttk.Label 默认不换行 —— 窗口一窄，右边就被硬生生切掉一句话
    （截图核对新布局时才看出来的）。这里把 wraplength 绑到实际宽度上，
    赋值前先比一次，避免「设 wraplength → 触发 <Configure> → 再设」自激。
    """

    def apply(event) -> None:
        wanted = max(minimum, event.width - 4)
        try:
            current = int(label.cget("wraplength") or 0)
        except (TypeError, ValueError, tk.TclError):
            current = -1
        if current != wanted:
            label.configure(wraplength=wanted)

    label.bind("<Configure>", apply)


class ConfigApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.env_path = env_store.ENV_FILE
        self.config, load_notes = env_store.load_config(self.env_path)

        # 抖音号 -> 浏览器配置目录 的对照表（profiles.json）
        self.profile_index, index_notes = profile_store.load_with_notes()
        folder_notes = self._attach_profile_folders()

        self._save_job = None
        self._loading = True
        self._syncing = False
        self._current_index = 0
        self._body_shown = None
        self.last_saved = ""
        self.orphans: list = []

        # 会话列表状态
        self.conversation_names: list = []   # 当前账号上次抓到的会话名
        self.conversation_at = ""            # 抓取时间（只做展示）
        self._conversation_owner = None      # 选择器里现在装的是哪个账号

        self._build_ui()
        self._load_into_ui()
        self._loading = False

        for note in list(load_notes) + list(index_notes) + folder_notes:
            self._set_message(note)
        self._refresh_preview()
        self._refresh_validation()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _attach_profile_folders(self) -> list:
        """把 .env 里的账号与 profiles.json 里的配置目录、指纹对应起来。

        .env 的 TASKS 里也会带一份指纹（生成时抄进去的），但**权威来源是
        profiles.json**：以它为先，只有它里面查不到时才退回来用 .env 里的值 ——
        这样 profiles.json 万一丢了也不会让指纹白白漂移一次。

        指纹缺失就**当场**分配并写回 —— 固定指纹的价值就在于「定下来之后永远
        不变」，拖到打开浏览器那一刻才分配，中间任何一次失败都会让指纹漂移。
        """
        missing: list = []
        for account in self.config.accounts:
            unique_id = account.unique_id.strip()
            from_env = account.fingerprint.strip()
            account.profile_folder = (
                profile_store.folder_for(self.profile_index, unique_id) if unique_id else ""
            )
            if not account.profile_folder:
                # 还没有配置目录（也就没有记录可写），先留着 .env 里的值
                account.fingerprint = from_env
                missing.append(account.display_name)
                continue
            account.fingerprint = profile_store.ensure_fingerprint(
                unique_id, account.profile_folder, fallback=from_env
            )

        # ensure_fingerprint 为了给旧账号补字段会写盘，本地缓存跟着更新一次
        self.profile_index = profile_store.load()

        notes: list = []
        if missing:
            shown = "、".join(missing[:3]) + ("…" if len(missing) > 3 else "")
            notes.append(
                f"{len(missing)} 个账号还没有浏览器配置目录（{shown}），"
                "点「刷新登录信息」会为它分配一个新目录"
            )
        return notes

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        self.root.title("DouYinSparkFlow 配置生成器")
        self.root.minsize(940, 620)

        header = ttk.Frame(self.root, padding=(16, 12, 16, 8))
        header.pack(fill="x")
        ttk.Label(header, text="DouYinSparkFlow 配置生成器", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ttk.Label(
            header,
            text="所有改动都会自动写入本目录下的 .env —— 左侧预览的就是真实写入内容",
            font=FONT_UI,
            foreground="#5F5E5A",
        ).pack(anchor="w", pady=(2, 0))

        panes = ttk.PanedWindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        left = ttk.Frame(panes, width=340)
        panes.add(left, weight=0)
        right = ttk.Frame(panes)
        panes.add(right, weight=1)

        self._build_preview(left)
        self._build_tabs(right)
        self._build_status()

    # -- 左侧：环境变量预览 -------------------------------------------------
    def _build_preview(self, master) -> None:
        ttk.Label(master, text="环境变量预览", font=("Microsoft YaHei UI", 11, "bold")).pack(
            anchor="w", padx=12, pady=(12, 6)
        )

        holder = ttk.Frame(master)
        holder.pack(fill="both", expand=True, padx=12)

        self.preview = ttk.Treeview(holder, columns=("key", "value"), show="headings", height=14)
        self.preview.heading("key", text="变量名")
        self.preview.heading("value", text="变量值")
        self.preview.column("key", width=150, stretch=False)
        self.preview.column("value", width=150, stretch=True)
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.preview.pack(side="left", fill="both", expand=True)
        self.preview.bind("<Double-1>", lambda _event: self.on_preview_detail())

        row = ttk.Frame(master)
        row.pack(fill="x", padx=12, pady=(6, 0))
        ttk.Button(row, text="复制变量名", width=11, command=self.on_copy_key).pack(side="left")
        ttk.Button(row, text="复制变量值", width=11, command=self.on_copy_value).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(row, text="查看详情", width=10, command=self.on_preview_detail).pack(
            side="left", padx=(6, 0)
        )

        ttk.Separator(master, orient="horizontal").pack(fill="x", padx=12, pady=12)

        ttk.Button(master, text="保存并写入 .env", command=lambda: self.save_now(force=True)).pack(
            fill="x", padx=12
        )
        ttk.Button(master, text="复制 .env 内容", command=self.on_copy_env).pack(
            fill="x", padx=12, pady=(6, 0)
        )
        ttk.Button(master, text="打开程序目录", command=self.on_open_env_dir).pack(
            fill="x", padx=12, pady=(6, 12)
        )

    # -- 右侧：标签页 -------------------------------------------------------
    def _build_tabs(self, master) -> None:
        self.notebook = ttk.Notebook(master)
        self.notebook.pack(fill="both", expand=True, padx=(10, 12), pady=12)

        base_tab = ttk.Frame(self.notebook)
        account_tab = ttk.Frame(self.notebook)
        self.notebook.add(base_tab, text="  基础配置  ")
        self.notebook.add(account_tab, text="  账户配置  ")

        self._build_base_tab(base_tab)
        self._build_account_tab(account_tab)

    def _build_base_tab(self, master) -> None:
        scroller = ScrollFrame(master)
        scroller.pack(fill="both", expand=True)
        form = scroller.inner

        def label(text: str, hint: str = "") -> None:
            ttk.Label(form, text=text, font=FONT_UI).pack(anchor="w", pady=(10, 2))
            if hint:
                ttk.Label(
                    form, text=hint, font=("Microsoft YaHei UI", 9), foreground="#888780"
                ).pack(anchor="w")

        label("代理地址", "当前代码未实际使用，可留空")
        self.var_proxy = tk.StringVar()
        ttk.Entry(form, textvariable=self.var_proxy, font=FONT_UI).pack(fill="x")

        label("执行时间", "仅 Docker 部署有效；写入 .env 时会拆成 CRON_HOUR / MINUTE / SECOND")
        time_row = ttk.Frame(form)
        time_row.pack(fill="x")
        self.var_hour = tk.StringVar()
        self.var_minute = tk.StringVar()
        self.var_second = tk.StringVar()
        for var, top in ((self.var_hour, 23), (self.var_minute, 59), (self.var_second, 59)):
            ttk.Spinbox(
                time_row, from_=0, to=top, width=4, format="%02.0f", textvariable=var, font=FONT_UI
            ).pack(side="left")
            ttk.Label(time_row, text=":", font=FONT_UI).pack(side="left", padx=2)
        ttk.Label(time_row, text="（时 : 分 : 秒）", font=("Microsoft YaHei UI", 9), foreground="#888780").pack(
            side="left", padx=(8, 0)
        )

        label("时区")
        self.var_tz = tk.StringVar()
        ttk.Combobox(form, textvariable=self.var_tz, values=TZ_OPTIONS, font=FONT_UI).pack(fill="x")
        self.var_tz.trace_add("write", self._on_field_changed)

        label("消息模板", "用回车换行即可，写入 .env 时会自动转成 \\n（core/tasks.py 按 \\n 拆分发送）")
        self.template_text = tk.Text(form, height=5, wrap="word", font=FONT_UI, relief="solid", borderwidth=1)
        self.template_text.pack(fill="x")
        self.template_text.bind("<<Modified>>", self._on_template_modified)

        label("一言类型", "[API] 会替换为这些类型里的随机一言")
        hitokoto_box = ttk.Frame(form)
        hitokoto_box.pack(fill="x")
        self.hitokoto_vars: dict = {}
        for index, option in enumerate(HITOKOTO_OPTIONS):
            var = tk.BooleanVar()
            box = ttk.Checkbutton(
                hitokoto_box,
                text=option,
                variable=var,
                command=self._on_field_changed,
            )
            box.grid(row=index // 4, column=index % 4, sticky="w", padx=(0, 14), pady=2)
            self.hitokoto_vars[option] = var

        label("浏览器操作最长等待时间", "毫秒，默认即可")
        self.var_timeout = tk.IntVar()
        ttk.Spinbox(
            form,
            from_=BROWSER_TIMEOUT_RANGE[0],
            to=BROWSER_TIMEOUT_RANGE[1],
            increment=1000,
            textvariable=self.var_timeout,
            font=FONT_UI,
        ).pack(fill="x")

        label("好友列表等待时间", "毫秒，网络慢可调大")
        self.var_friend_wait = tk.IntVar()
        ttk.Spinbox(
            form,
            from_=FRIEND_LIST_WAIT_RANGE[0],
            to=FRIEND_LIST_WAIT_RANGE[1],
            increment=1000,
            textvariable=self.var_friend_wait,
            font=FONT_UI,
        ).pack(fill="x")

        label("任务重试次数")
        self.var_retry = tk.IntVar()
        ttk.Spinbox(
            form,
            from_=RETRY_TIMES_RANGE[0],
            to=RETRY_TIMES_RANGE[1],
            increment=1,
            textvariable=self.var_retry,
            font=FONT_UI,
        ).pack(fill="x")

        label("输出日志级别", "Error < Warning < Info < Debug，越小输出越少")
        self.var_log_level = tk.StringVar()
        ttk.Combobox(
            form, textvariable=self.var_log_level, values=LOG_LEVEL_OPTIONS, font=FONT_UI
        ).pack(fill="x")
        self.var_log_level.trace_add("write", self._on_field_changed)

        for var in (
            self.var_proxy,
            self.var_hour,
            self.var_minute,
            self.var_second,
            self.var_timeout,
            self.var_friend_wait,
            self.var_retry,
        ):
            var.trace_add("write", self._on_field_changed)

    def _build_account_tab(self, master) -> None:
        # 底部消息条（空状态和列表状态都要能看到，所以先按 side=bottom 占位）
        self.message_var = tk.StringVar(value="")
        ttk.Label(
            master, textvariable=self.message_var, font=FONT_UI, foreground="#5F5E5A"
        ).pack(side="bottom", anchor="w", fill="x", padx=16, pady=(8, 14))

        # -- 空状态：一个账号都没有时只显示引导 -----------------------------
        self.account_empty = ttk.Frame(master)
        empty_box = ttk.Frame(self.account_empty)
        empty_box.place(relx=0.5, rely=0.42, anchor="center")
        ttk.Label(
            empty_box,
            text="还没有任何账号",
            font=("Microsoft YaHei UI", 15, "bold"),
            foreground="#5F5E5A",
        ).pack()
        ttk.Label(
            empty_box,
            text=(
                "点下面的按钮会自动打开浏览器 ——\n"
                "在弹出的窗口里完成登录，工具会自动抓取 Cookie、用户名和抖音号，\n"
                "不需要手填任何信息，完成后浏览器和窗口都会自动关闭。"
            ),
            font=FONT_UI,
            foreground="#888780",
            justify="center",
        ).pack(pady=(12, 0))
        ttk.Button(empty_box, text="＋  添加账号", width=18, command=self.on_add_account).pack(
            pady=(20, 0)
        )

        # -- 有账号时显示的列表 + 详情 ---------------------------------------
        # 详情区多了「会话列表」之后整体变高，窗口调小/缩放放大时容易顶出去，
        # 所以套一层滚动容器承载。
        self.account_body = ScrollFrame(master)
        sheet = self.account_body.inner

        top = ttk.Frame(sheet, padding=(0, 6, 0, 0))
        top.pack(fill="x")
        ttk.Label(top, text="账号管理", font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        ttk.Button(top, text="＋ 添加账号", width=13, command=self.on_add_account).pack(side="right")
        ttk.Label(
            top,
            text="账号信息由浏览器登录后自动获取，不能手工修改",
            font=("Microsoft YaHei UI", 9),
            foreground="#888780",
        ).pack(side="right", padx=(0, 12))

        holder = ttk.Frame(sheet, padding=(0, 8, 0, 0))
        holder.pack(fill="x")
        self.account_tree = ttk.Treeview(
            holder,
            columns=("username", "unique_id", "cookies", "targets"),
            show="headings",
            height=4,
            selectmode="browse",
        )
        for key, text, width in (
            ("username", "用户名", 150),
            ("unique_id", "抖音号", 170),
            ("cookies", "登录信息", 200),
            ("targets", "目标好友", 90),
        ):
            self.account_tree.heading(key, text=text)
            self.account_tree.column(key, width=width, anchor="w")
        self.account_tree.pack(fill="x")
        self.account_tree.bind("<<TreeviewSelect>>", self._on_account_select)

        # -- 目标好友：唯一的输入方式就是从会话列表里勾选 ---------------------
        # 早先是「手填标签框 + 会话列表」两套控件并排写着同一个字段，
        # 用户改完自己都说不清哪个算数；现在合并成一个可搜索的勾选列表。
        #
        # 位置放在「账号详情」**之前**，而且是它的兄弟节点：
        #   1. 这是每天真正要动的唯一一处，摆在账号列表正下方不用滚就能摸到；
        #   2. 「账号详情」那个框写着「只读」，把可编辑的选择器塞进去语义不通 ——
        #      截图核对时一眼就看出来了。
        friends = ttk.LabelFrame(sheet, text=" 目标好友 —— 从会话列表里勾选 ", padding=10)
        friends.pack(fill="x", pady=(12, 0))
        hint = ttk.Label(
            friends,
            text=(
                "会话名来自抖音「消息」页的会话列表（有备注名就用备注名）。"
                "点名字即勾选 / 取消，选完自动写进 .env。"
            ),
            font=("Microsoft YaHei UI", 9),
            foreground="#888780",
            justify="left",
        )
        hint.pack(anchor="w", fill="x")
        _wrap_to_width(hint)
        self.picker = ConversationPicker(
            friends, on_change=self._on_targets_changed, on_fetch=self.on_fetch_conversations
        )
        self.picker.pack(fill="both", expand=True, pady=(8, 0))

        # -- 账号详情：纯只读的参照信息 --------------------------------------
        detail = ttk.LabelFrame(sheet, text=" 账号详情（只读，来自抖音接口） ", padding=14)
        detail.pack(fill="x", pady=(10, 6))

        grid = ttk.Frame(detail)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        def readonly(row: int, column: int, label: str, var: tk.StringVar, mono=False, span=1):
            ttk.Label(grid, text=label, font=FONT_UI).grid(
                row=row, column=column, sticky="w", pady=3
            )
            entry = ttk.Entry(
                grid,
                textvariable=var,
                font=FONT_MONO if mono else FONT_UI,
                state="readonly",
            )
            entry.grid(
                row=row, column=column + 1, columnspan=span, sticky="ew", padx=(8, 12), pady=3
            )
            return entry

        self.var_acc_username = tk.StringVar()
        self.var_acc_unique_id = tk.StringVar()
        self.var_acc_folder = tk.StringVar()
        self.var_acc_fingerprint = tk.StringVar()
        self.var_acc_login = tk.StringVar()

        readonly(0, 0, "用户名", self.var_acc_username)
        readonly(0, 2, "抖音号", self.var_acc_unique_id)
        readonly(1, 0, "配置目录", self.var_acc_folder, mono=True)
        readonly(1, 2, "浏览器指纹", self.var_acc_fingerprint, mono=True)
        readonly(2, 0, "登录信息", self.var_acc_login, span=3)

        fp_hint = ttk.Label(
            grid,
            text=(
                "指纹固定写在 profiles.json 里，每次打开浏览器都传同一个值 —— "
                "否则隐身浏览器每次随机一个指纹，在风控眼里就是同一个人不停换设备"
            ),
            font=("Microsoft YaHei UI", 9),
            foreground="#888780",
            justify="left",
        )
        # sticky 必须是 "ew" 而不是 "w"：_wrap_to_width 靠 <Configure> 拿宽度，
        # 用 "w" 时 label 只占内容宽度、永远停在最小换行宽度上（截图核对才发现，
        # 文字被挤成细长一列）。撑满跨列区域后，第一次事件里就是真实可用宽度。
        fp_hint.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        _wrap_to_width(fp_hint)

        self.var_cookies_key = tk.StringVar(value="COOKIES_")
        ttk.Label(
            detail,
            textvariable=self.var_cookies_key,
            font=FONT_MONO,
            foreground="#185FA5",
        ).pack(anchor="w", pady=(10, 0))
        ttk.Label(
            detail,
            text="Cookies（登录后自动回填，只读）",
            font=FONT_UI,
        ).pack(anchor="w", pady=(6, 2))
        # height 压到 2 行：这里基本只是「有没有 cookie」的确认，真要细看有滚动条
        self.cookies_preview = ScrolledText(detail, height=2, mono=True)
        self.cookies_preview.container.pack(fill="both", expand=True)

        actions = ttk.Frame(detail)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(
            actions, text="刷新登录信息", width=13, command=self.on_refresh_account
        ).pack(side="left")
        ttk.Button(
            actions, text="打开配置目录", width=12, command=self.on_open_profile_dir
        ).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="复制 Cookies", width=11, command=self.on_copy_cookies).pack(
            side="left", padx=(8, 0)
        )
        self.btn_clean_orphans = ttk.Button(
            actions, text="清理旧变量", width=10, command=self.on_clean_orphans, state="disabled"
        )
        self.btn_clean_orphans.pack(side="left", padx=(8, 0))

        # 「移除账户」单独一行、靠右：
        #   1. 它是这里唯一的破坏性操作，跟上面几个「刷新 / 打开 / 复制」分开更不容易点错；
        #   2. 原来五个挤一行，按钮请求宽度合计 657px 而详情区只有 542px ——
        #      side="right" 的它会被整个挤出可视区（实测 winfo_ismapped() == False，
        #      用户既看不到也点不到），连「清理旧 Cookie 变量」都被切掉半个字。
        danger = ttk.Frame(detail)
        danger.pack(fill="x", pady=(8, 0))
        ttk.Button(danger, text="移除账户", width=10, command=self.on_remove_account).pack(
            side="right"
        )

    def _refresh_account_view(self) -> None:
        """有账号就显示列表，没有就显示空状态引导。"""
        show_body = bool(self.config.accounts)
        if show_body == self._body_shown:
            return
        self._body_shown = show_body
        if show_body:
            self.account_empty.pack_forget()
            self.account_body.pack(fill="both", expand=True)
        else:
            self.account_body.pack_forget()
            self.account_empty.pack(fill="both", expand=True)

    def _build_status(self) -> None:
        bar = ttk.Frame(self.root, padding=(16, 8))
        bar.pack(fill="x", side="bottom")
        ttk.Separator(self.root, orient="horizontal").pack(fill="x", side="bottom")

        self.save_var = tk.StringVar(value="尚未保存")
        ttk.Label(bar, textvariable=self.save_var, font=FONT_UI, foreground="#5F5E5A").pack(side="left")

        self.issue_var = tk.StringVar(value="校验通过")
        self.issue_label = ttk.Label(bar, textvariable=self.issue_var, font=FONT_UI, foreground="#0F6E56")
        self.issue_label.pack(side="left", padx=(16, 0))
        ttk.Button(bar, text="查看校验结果", width=13, command=self.on_show_issues).pack(
            side="left", padx=(8, 0)
        )

        ttk.Label(bar, text=f".env: {self.env_path}", font=("Consolas", 9), foreground="#888780").pack(
            side="right"
        )

    # -------------------------------------------------------------- 载入界面
    def _load_into_ui(self) -> None:
        self._syncing = True
        config = self.config

        self.var_proxy.set(config.proxy_address)
        hour, minute, second = split_run_time(config.run_time)
        self.var_hour.set(hour)
        self.var_minute.set(minute)
        self.var_second.set(second)
        self.var_tz.set(config.tz)
        self.template_text.delete("1.0", "end")
        self.template_text.insert("1.0", config.message_template)
        self.template_text.edit_modified(False)
        for option, var in self.hitokoto_vars.items():
            var.set(option in config.hitokoto_types)
        self.var_timeout.set(int(config.browser_timeout))
        self.var_friend_wait.set(int(config.friend_list_wait_time))
        self.var_retry.set(int(config.task_retry_times))
        self.var_log_level.set(config.log_level)

        self._refresh_account_tree(select=self._current_index)
        self._syncing = False

    # ------------------------------------------------------------ 收集与保存
    def collect_from_ui(self) -> None:
        config = self.config
        config.proxy_address = self.var_proxy.get().strip()
        config.run_time = build_run_time(
            self.var_hour.get(), self.var_minute.get(), self.var_second.get()
        )
        config.tz = self.var_tz.get().strip() or "Asia/Shanghai"
        config.message_template = self.template_text.get("1.0", "end-1c")
        config.hitokoto_types = [name for name, var in self.hitokoto_vars.items() if var.get()]
        config.browser_timeout = self._safe_int(
            self.var_timeout, config.browser_timeout, *BROWSER_TIMEOUT_RANGE
        )
        config.friend_list_wait_time = self._safe_int(
            self.var_friend_wait, config.friend_list_wait_time, *FRIEND_LIST_WAIT_RANGE
        )
        config.task_retry_times = self._safe_int(
            self.var_retry, config.task_retry_times, *RETRY_TIMES_RANGE
        )
        config.log_level = self.var_log_level.get().strip() or "Info"

    @staticmethod
    def _safe_int(var: tk.IntVar, fallback: int, low: int, high: int) -> int:
        try:
            value = int(var.get())
        except (tk.TclError, ValueError):
            return fallback
        return max(low, min(high, value))

    def _on_field_changed(self, *_args) -> None:
        if self._syncing or self._loading:
            return
        self.collect_from_ui()
        self._on_data_changed()

    def _on_template_modified(self, _event=None) -> None:
        if not self.template_text.edit_modified():
            return
        self.template_text.edit_modified(False)
        if self._syncing or self._loading:
            return
        self.config.message_template = self.template_text.get("1.0", "end-1c")
        self._on_data_changed()

    def _on_targets_changed(self) -> None:
        """选择器里勾选变化 → 写回 account.targets。

        这是目标好友**唯一**的写入点。以前手填标签框和会话列表各写一次，
        两边都要小心别把对方冲掉；现在只有一处，不用再对账。
        """
        if self._syncing or self._loading:
            return
        account = self.current_account()
        if account is None:
            return
        account.targets = self.picker.get_selected()
        self._on_data_changed()

    def _on_data_changed(self) -> None:
        self._refresh_preview()
        self._refresh_account_tree(select=self._current_index)
        self._refresh_validation()
        self.schedule_save()

    def schedule_save(self) -> None:
        if self._loading:
            return
        self.save_var.set("有未保存的改动…")
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(AUTOSAVE_DELAY_MS, lambda: self.save_now())

    def save_now(self, force: bool = False) -> None:
        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
            self._save_job = None

        if self._loading:
            return

        self.collect_from_ui()
        try:
            notes, orphans = env_store.save_config(self.config, self.env_path)
        except Exception as exc:
            self.save_var.set(f"保存失败：{type(exc).__name__}: {exc}")
            if force:
                messagebox.showerror("保存失败", f"{type(exc).__name__}: {exc}")
            return

        self.orphans = orphans
        self.last_saved = datetime.now().strftime("%H:%M:%S")
        self.save_var.set(f"已自动保存 {self.last_saved}")
        self._refresh_preview()
        self._refresh_validation()
        if orphans:
            self.btn_clean_orphans.config(state="normal")
            self._set_message(
                f"检测到 {len(orphans)} 个未使用的旧 Cookie 变量（可能是改过抖音号留下的）"
            )
        else:
            self.btn_clean_orphans.config(state="disabled")

        if force:
            self._set_message(f"已写入 {self.env_path.name}")

    # ------------------------------------------------------------ 预览与校验
    def _refresh_preview(self) -> None:
        env_map = self.config.to_env_map()
        selected = self.preview.selection()
        keep = selected[0] if selected else None

        self.preview.delete(*self.preview.get_children())
        for key, value in env_map.items():
            shown = value if len(value) <= 60 else value[:57] + "…"
            self.preview.insert("", "end", iid=key, values=(key, shown))

        if keep and self.preview.exists(keep):
            self.preview.selection_set(keep)

    def _refresh_validation(self) -> None:
        self.issues = validate(self.config)
        errors = sum(1 for level, _ in self.issues if level == "错误")
        warnings = sum(1 for level, _ in self.issues if level == "警告")
        if errors:
            self.issue_var.set(f"校验：{errors} 个错误 / {warnings} 个警告")
            self.issue_label.configure(foreground="#A32D2D")
        elif warnings:
            self.issue_var.set(f"校验：{warnings} 个警告")
            self.issue_label.configure(foreground="#854F0B")
        else:
            self.issue_var.set("校验通过")
            self.issue_label.configure(foreground="#0F6E56")

    def _set_message(self, text: str) -> None:
        self.message_var.set(text)

    # ---------------------------------------------------------------- 账户表
    def _refresh_account_tree(self, select: int | None = None) -> None:
        self.account_tree.delete(*self.account_tree.get_children())
        for index, account in enumerate(self.config.accounts):
            record = (
                profile_store.record_for(self.profile_index, account.unique_id)
                if account.unique_id
                else {}
            )
            detail = account.cookie_status
            if record.get("updated_at"):
                detail = f"{detail} · {record['updated_at'][5:16]}"
            self.account_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    account.username or "（未知）",
                    account.unique_id or "（未知）",
                    detail,
                    f"{len(account.targets)} 个",
                ),
            )
        if select is not None and 0 <= select < len(self.config.accounts):
            self._current_index = select
            self.account_tree.selection_set(str(select))
        elif self.config.accounts:
            self._current_index = 0
            self.account_tree.selection_set("0")
        else:
            self._current_index = -1
        self._refresh_account_view()
        self._sync_detail_from_account()

    def _on_account_select(self, _event=None) -> None:
        selection = self.account_tree.selection()
        if not selection:
            return
        self._current_index = int(selection[0])
        self._sync_detail_from_account()

    def _login_summary(self, account: Account) -> str:
        parts = [account.cookie_status]
        if not account.profile_folder:
            parts.append("尚无浏览器配置目录")
        else:
            record = profile_store.record_for(self.profile_index, account.unique_id)
            if record.get("updated_at"):
                parts.append(f"最近刷新 {record['updated_at']}")
            if record.get("uid"):
                parts.append(f"uid {record['uid']}")
        return " · ".join(parts)

    def _sync_detail_from_account(self) -> None:
        """把当前账号的数据推给各控件（模型 → 界面，单向）。"""
        self._syncing = True
        try:
            account = self.current_account()
            if account is None:
                self.var_acc_username.set("")
                self.var_acc_unique_id.set("")
                self.var_acc_folder.set("")
                self.var_acc_fingerprint.set("")
                self.var_acc_login.set("")
                self.cookies_preview.set_text("")
                self.var_cookies_key.set("COOKIES_")
            else:
                self.var_acc_username.set(account.username or "（未知）")
                self.var_acc_unique_id.set(account.unique_id or "（未知）")
                self.var_acc_folder.set(profile_store.describe(account.profile_folder))
                self.var_acc_fingerprint.set(
                    f"--fingerprint={account.fingerprint}"
                    if account.fingerprint
                    else "（还没有记录，首次打开浏览器时分配）"
                )
                self.var_acc_login.set(self._login_summary(account))
                self.cookies_preview.set_text(account.cookies or "（还没有 Cookies）")
                self.var_cookies_key.set(f"{account.cookies_key}  ← 写入 .env 的键名")
        finally:
            self._syncing = False

        self._refresh_conversation_list()
        if account is not None:
            # 模型可能被别处改过（比如重复抖音号合并账号时 targets 被覆盖），
            # set_selected 内部有幂等判断，一致时不会白重画
            self.picker.set_selected(list(account.targets))

    def current_account(self) -> Account | None:
        if 0 <= self._current_index < len(self.config.accounts):
            return self.config.accounts[self._current_index]
        return None

    # ------------------------------------------------------------ 会话列表
    def _refresh_conversation_list(self, force: bool = False) -> None:
        """把该账号上次抓到的会话名交给选择器，并回显已勾选的目标好友。

        force=False 时只在「换了账号」才整体重设 —— 每次勾选都会绕到
        _on_data_changed → _refresh_account_tree → 这里，若无条件重设，
        列表会被重画，滚动位置每次都弹回顶部，没法连续挑人。
        """
        account = self.current_account()
        owner = account.unique_id.strip() if account is not None else ""
        rebuild = force or owner != self._conversation_owner
        self._conversation_owner = owner

        if account is None:
            self.conversation_names = []
            self.conversation_at = ""
            self.picker.set_data([], [])
            self.picker.set_fetch_enabled(False)
            self.picker.set_status("还没有任何账号，先点「＋ 添加账号」")
            return

        if rebuild:
            if owner:
                self.conversation_names = profile_store.conversations_for(
                    self.profile_index, owner
                )
                self.conversation_at = profile_store.conversations_at_for(
                    self.profile_index, owner
                )
            else:
                self.conversation_names = []
                self.conversation_at = ""
            self.picker.set_data(self.conversation_names, list(account.targets))

        # 没有配置目录（或目录已经不在磁盘上）就没法用它的登录态开浏览器，
        # 按钮直接禁掉，比让用户点一下再弹「请先刷新登录信息」省事
        folder = str(account.profile_folder or "").strip()
        self.picker.set_fetch_enabled(
            bool(folder) and profile_store.profile_dir(folder).is_dir()
        )
        self.picker.set_status(self._conversation_summary(account))

    def _conversation_summary(self, account) -> str:
        folder = str(getattr(account, "profile_folder", "") or "").strip()
        if not folder:
            return "还没有拉取过会话列表 —— 先点「刷新登录信息」登录一次，才能拉取"
        if not profile_store.profile_dir(folder).is_dir():
            return "浏览器配置目录不在了 —— 点「刷新登录信息」重新登录一次，才能拉取"
        if not self.conversation_names:
            return "还没有拉取过会话列表 —— 点右侧「拉取会话列表」自动抓取"
        parts = [f"已抓取 {len(self.conversation_names)} 个会话"]
        if self.conversation_at:
            parts.append(f"最近拉取 {self.conversation_at[5:16]}")
        outside = [t for t in account.targets if t not in set(self.conversation_names)]
        if outside:
            parts.append(f"另有 {len(outside)} 个已选好友不在列表里（列表中标黄）")
        return " · ".join(parts)

    # ---------------------------------------------------------------- 账户操作
    def on_add_account(self) -> None:
        """添加账号：先不写进列表，等浏览器登录成功拿到抖音号再落库。

        这样中途取消不会在 .env 里留下一条空账号（空抖音号会让 TASKS 里多出
        一条没用的任务，还会让校验一路报错）。
        """
        account = Account()
        self._set_message("正在打开浏览器 —— 登录完成后会自动抓取并添加，中途取消不会留下空账号")
        self._open_login_dialog(account, mode="add")

    def on_refresh_account(self) -> None:
        """刷新登录信息：用该账号原来的浏览器配置目录重开浏览器。"""
        account = self.current_account()
        if account is None:
            messagebox.showinfo("还没有账号", "请先点「＋ 添加账号」。")
            return
        if self._save_job is not None:
            self.save_now()  # 先落盘，避免浏览器开着时改动的配置被回滚
        self._set_message(
            f"正在用「{profile_store.describe(account.profile_folder)}」打开浏览器，"
            "登录态有效时会直接刷新 Cookie"
        )
        self._open_login_dialog(account, mode="refresh")

    def _open_login_dialog(self, account: Account, mode: str = "add") -> None:
        taken = [item.unique_id for item in self.config.accounts if item.unique_id.strip()]
        try:
            # 不需要模态：浏览器窗口本来就是独立的，用户也可以同时回主界面看别的账号
            LoginDialog(
                self.root, account, on_saved=self._on_login_saved, mode=mode, taken_ids=taken
            )
        except Exception as exc:
            messagebox.showerror("打不开登录窗口", f"{type(exc).__name__}: {exc}")

    def _on_login_saved(self, account: Account, mode: str = "add") -> None:
        """登录窗口抓到信息后回到这里：更新列表、写 .env、刷新界面。"""
        self.profile_index = profile_store.load()

        # 刷新模式：对象本来就在列表里，原地更新即可
        if any(item is account for item in self.config.accounts):
            index = self.config.accounts.index(account)
            self._set_message(
                f"已刷新「{account.display_name}」的登录信息（{account.cookie_status}），"
                f"键名 {account.cookies_key}"
            )
        else:
            duplicate = None
            if account.unique_id:
                for item in self.config.accounts:
                    if item.unique_id.strip().upper() == account.unique_id.strip().upper():
                        duplicate = item
                        break
            if duplicate is not None:
                # 同一个抖音号重复添加 → 更新那一条，不新增（键名会互相覆盖）
                duplicate.username = account.username or duplicate.username
                duplicate.cookies = account.cookies or duplicate.cookies
                duplicate.profile_folder = account.profile_folder or duplicate.profile_folder
                duplicate.fingerprint = account.fingerprint or duplicate.fingerprint
                if account.targets:
                    duplicate.targets = list(account.targets)
                index = self.config.accounts.index(duplicate)
                self._set_message(
                    f"抖音号 {account.unique_id} 已在列表里，已更新「{duplicate.display_name}」"
                    f"的登录信息（{duplicate.cookie_status}）"
                )
            else:
                self.config.accounts.append(account)
                index = len(self.config.accounts) - 1
                self._set_message(
                    f"已添加「{account.display_name}」（抖音号 {account.unique_id}）"
                    f"，配置目录 {profile_store.describe(account.profile_folder)}"
                )

        self._refresh_account_tree(select=index)
        self._refresh_validation()
        self.save_now()

    # -------------------------------------------------------------- 拉取会话列表
    def on_fetch_conversations(self) -> None:
        """用该账号的浏览器配置打开抖音，滚动收集全部会话名。

        打开的是这个账号**自己**的配置目录（登录态在里面），所以不需要重新登录；
        浏览器默认无头运行，用户碰不到它，也就不会误操作把列表滚乱。
        """
        account = self.current_account()
        if account is None:
            self._set_message("请先在列表里选中一个账号")
            return

        folder = str(account.profile_folder or "").strip()
        if not folder:
            messagebox.showinfo(
                "还没有浏览器配置目录",
                "这个账号还没有浏览器配置目录，没法用它的登录态打开浏览器。\n\n"
                "请先点「刷新登录信息」登录一次，之后就能直接拉取会话列表了。",
            )
            return

        target = profile_store.profile_dir(folder)
        if not target.is_dir():
            messagebox.showinfo(
                "配置目录不存在",
                f"{target}\n\n"
                "profiles.json 里记着这个目录，但磁盘上已经没有了。\n"
                "请先点「刷新登录信息」重新创建（需要重新登录）。",
            )
            return

        if self._save_job is not None:
            self.save_now()  # 先落盘，免得浏览器跑着的时候改动的配置被回滚

        self._set_message(
            f"正在用「{profile_store.describe(folder)}」拉取「{account.display_name}」的会话列表…"
        )
        try:
            ConversationDialog(self.root, account, on_fetched=self._on_conversations_fetched)
        except Exception as exc:
            messagebox.showerror("打不开拉取窗口", f"{type(exc).__name__}: {exc}")

    def _on_conversations_fetched(self, account: Account, names: list) -> None:
        """抓完回到这里：写进 profiles.json，把候选列表交给选择器。"""
        try:
            profile_store.set_conversations(
                account.unique_id, names, folder=account.profile_folder
            )
        except Exception as exc:
            messagebox.showerror(
                "保存会话列表失败",
                f"{type(exc).__name__}: {exc}\n\n"
                "会话名没能写进 profiles.json，界面里的候选列表不会保留。",
            )
            return

        self.profile_index = profile_store.load()

        if self.current_account() is account:
            self._refresh_conversation_list(force=True)
            self._set_message(
                f"已拉取「{account.display_name}」的 {len(names)} 个会话 —— "
                "在「目标好友」里点名字勾选"
            )
        else:
            # 拉取期间用户切到了别的账号：数据已存好，但不要抢走当前的界面
            self._set_message(
                f"已拉取「{account.display_name}」的 {len(names)} 个会话"
                "（当前显示的是另一个账号）"
            )

    def on_remove_account(self) -> None:
        account = self.current_account()
        if account is None:
            return
        label = account.display_name
        if not messagebox.askyesno(
            "移除账户",
            f"确定移除「{label}」吗？\n\n"
            f"· {account.cookies_key} 会从 .env 里删掉\n"
            f"· 浏览器配置目录会保留（下次再加同一个账号可以直接复用登录态）",
        ):
            return

        key = account.cookies_key
        folder = str(account.profile_folder or "").strip()
        unique_id = account.unique_id.strip()

        # 是否连浏览器配置目录一起删 —— 这是唯一会动磁盘内容的地方，默认不删
        if folder:
            delete_profile = messagebox.askyesno(
                "要连浏览器配置目录一起删掉吗？",
                f"{profile_store.profile_dir(folder)}\n\n"
                "· 选「否」：目录留着，将来重新添加这个账号可以直接复用登录态（推荐）\n"
                "· 选「是」：占用空间释放，但这个账号将来必须重新扫码登录",
                default=messagebox.NO,
                icon="question",
            )
            if delete_profile:
                ok, detail = profile_store.remove_profile_dir(folder)
                if ok:
                    profile_store.forget(unique_id)
                    self.profile_index = profile_store.load()
                    self._set_message(f"已删除配置目录：{detail}")
                else:
                    self._set_message(f"配置目录没删掉：{detail}")

        self.config.accounts.remove(account)
        if unique_id:
            env_store.unset_keys([key], self.env_path)
        index = max(0, min(self._current_index, len(self.config.accounts) - 1))
        self._refresh_account_tree(select=index)
        self._refresh_validation()
        self.save_now()
        if not self.config.accounts:
            self._set_message(f"已移除「{label}」—— 现在一个账号都没有了")

    def on_clean_orphans(self) -> None:
        if not self.orphans:
            return
        if not messagebox.askyesno(
            "清理旧 Cookie 变量",
            "将从 .env 中删除这些不再被任何账户引用的变量：\n\n" + "\n".join(self.orphans),
        ):
            return
        removed = env_store.unset_keys(self.orphans, self.env_path)
        self.orphans = []
        self.btn_clean_orphans.config(state="disabled")
        self._refresh_preview()
        self._set_message(f"已清理 {removed} 个未使用的 Cookie 变量")

    # ---------------------------------------------------------------- 预览操作
    def _selected_preview(self):
        selection = self.preview.selection()
        if not selection:
            return None, None
        key = selection[0]
        return key, self.config.to_env_map().get(key, "")

    def on_copy_key(self) -> None:
        key, _value = self._selected_preview()
        if key is None:
            self._set_message("请先在左侧选中一个变量")
            return
        self._copy(key, f"已复制变量名 {key}")

    def on_copy_value(self) -> None:
        key, value = self._selected_preview()
        if key is None:
            self._set_message("请先在左侧选中一个变量")
            return
        self._copy(value, f"已复制 {key} 的值")

    def on_copy_env(self) -> None:
        self.collect_from_ui()
        self._copy(self.config.render_env_text(), "已复制 .env 内容到剪贴板")

    def on_copy_cookies(self) -> None:
        account = self.current_account()
        if account is None or not account.cookies:
            self._set_message("该账户还没有 Cookies")
            return
        self._copy(account.cookies, f"已复制 {account.cookies_key} 的值")

    def on_preview_detail(self) -> None:
        key, value = self._selected_preview()
        if key is None:
            self._set_message("请先在左侧选中一个变量")
            return
        window = tk.Toplevel(self.root)
        window.title(f"{key} 详情")
        window.transient(self.root)
        window.geometry("620x360")
        box = ScrolledText(window, height=20, mono=True)
        box.container.pack(fill="both", expand=True, padx=10, pady=10)
        box.set_text(value)
        ttk.Button(window, text="关闭", command=window.destroy).pack(pady=(0, 10))

    def on_show_issues(self) -> None:
        if not self.issues:
            messagebox.showinfo(
                "校验结果",
                "全部通过。\n\n"
                f"把 {self.env_path.name} 放到主程序的运行目录即可生效：\n"
                "  本地运行 → 项目根目录（与 main.py 同级）\n"
                "  Docker  → ./config/.env",
            )
            return
        lines = [f"[{level}] {message}" for level, message in self.issues]
        messagebox.showinfo("校验结果", "\n\n".join(lines))

    def on_open_env_dir(self) -> None:
        """打开程序目录（.env、profiles.json、profiles/ 都在这里）。

        .env 需要复制到主程序的运行目录才会生效，所以给一个直达入口。
        """
        self._open_folder(self.env_path.parent, "程序目录")

    def on_open_profile_dir(self) -> None:
        """打开当前账号的浏览器配置目录。"""
        account = self.current_account()
        if account is None:
            self._set_message("请先在列表里选中一个账号")
            return
        folder = str(account.profile_folder or "").strip()
        if not folder:
            messagebox.showinfo(
                "还没有配置目录",
                "这个账号还没有浏览器配置目录。\n"
                "点「刷新登录信息」会分配一个，登录成功后写入 profiles.json。",
            )
            return
        target = profile_store.profile_dir(folder)
        if not target.is_dir():
            messagebox.showinfo(
                "目录不存在",
                f"{target}\n\n"
                "profiles.json 里记着这个目录，但磁盘上没有了。\n"
                "点「刷新登录信息」会重新创建它（需要重新登录）。",
            )
            return
        self._open_folder(target, f"配置目录 {folder}")

    def _open_folder(self, target, label: str) -> None:
        try:
            target.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(target)  # noqa: S606 —— Windows 上即「在资源管理器中打开」
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
            self._set_message(f"已打开{label}：{target}")
        except Exception as exc:
            self._set_message(f"打不开目录：{type(exc).__name__}: {exc}")

    def _copy(self, text: str, message: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_message(message)

    # ------------------------------------------------------------------ 关闭
    def _on_close(self) -> None:
        try:
            if self._save_job is not None:
                self.save_now()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    if sys.platform == "win32":
        try:
            from ctypes import windll

            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass

    root.update_idletasks()
    # 账户页里「目标好友」那块列表不矮，700 高会把勾选区压在折线以下，
    # 所以默认给到能一屏放下的高度；屏幕不够就按屏幕截断，
    # 表单本身套了滚动容器，小屏也不会用不了。
    width = 1040
    height = min(880, max(620, root.winfo_screenheight() - 80))
    x = (root.winfo_screenwidth() - width) // 2
    y = max(0, (root.winfo_screenheight() - height) // 4)
    root.geometry(f"{width}x{height}+{max(0, x)}+{y}")

    app = ConfigApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
