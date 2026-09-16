"""可复用的小控件：可搜索的多选列表、只读文本区。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

FONT_UI = ("Microsoft YaHei UI", 10)
FONT_MONO = ("Consolas", 9)
FONT_HINT = ("Microsoft YaHei UI", 9)

HINT_GREY = "#888780"
PICK_BLUE = "#185FA5"
OUTSIDE_AMBER = "#854F0B"

CHECKED = "☑"
UNCHECKED = "☐"


def _unique_names(values) -> list:
    """去空、去重、保持首次出现的顺序。"""
    seen: set = set()
    out: list = []
    for value in values or []:
        name = str(value or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


class ConversationPicker(ttk.Frame):
    """可搜索的多选列表：从抓取到的会话列表里挑目标好友。

    为什么长成这样：
        「目标好友」只有一种来源（抖音「消息」页的会话列表），界面就应该只有
        一处能改它。早先是「手填标签框 + 会话列表」两套控件同时写着同一个字段，
        用户改完自己都说不清哪个算数 —— 现在收敛成一个：拉取 → 勾选。

    几个实现取舍（都不是随手写的）：

    - 用 Treeview 而不是 Listbox：Treeview 能整行改内容，勾选状态要逐行翻转；
      Listbox 只能删了重插，一勾选就把滚动位置弹回顶部。
    - 勾选自己画 ☑/☐，不用原生选中态（``selectmode="none"``）：
      原生高亮和「已勾选」是两回事，两套颜色叠在一起会看糊。
    - iid 用行号、另存 ``self._rows`` 做映射：会话名可能重名（两个好友同昵称），
      拿名字当 iid 会互相顶掉。
    - 勾选既不重画列表、也不重排候选：否则每点一下列表就跳一次，没法连着挑人。
    - 拖不出候选池：``_pool`` 只在 ``set_data`` 时重算，取消勾选的行先留着，
      免得手一抖名单就从眼皮底下消失。
    """

    def __init__(self, master, on_change=None, on_fetch=None, height: int = 7) -> None:
        super().__init__(master)
        self._on_change = on_change
        self._on_fetch = on_fetch
        self._fetched: list = []      # 抓取到的会话名（用来判断谁「不在列表里」）
        self._fetched_set: set = set()
        self._pool: list = []         # 候选池 = 抓取到的 + 老数据里要保留的
        self._selected: list = []     # 已选（顺序 = 加入顺序）
        self._rows: list = []         # 当前显示的行 -> 名字（iid 就是行号）
        self._rebuilding = False

        header = ttk.Frame(self)
        header.pack(fill="x")
        self.status_var = tk.StringVar(value="还没有拉取过会话列表")
        ttk.Label(
            header, textvariable=self.status_var, font=FONT_UI, foreground="#5F5E5A"
        ).pack(side="left", anchor="w")
        self.btn_fetch = ttk.Button(
            header, text="拉取会话列表", width=13, command=self._fetch
        )
        self.btn_fetch.pack(side="right")

        search_row = ttk.Frame(self)
        search_row.pack(fill="x", pady=(8, 4))
        ttk.Label(search_row, text="搜索", font=FONT_UI).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_args: self._rebuild())
        # 留个引用：这是详情区里唯一的可编辑输入框（其余账号字段都是只读的），
        # 回归脚本要按「非账号信息」把它排除掉
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_var, font=FONT_UI)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(8, 10))
        self.count_var = tk.StringVar(value="已选 0 个")
        ttk.Label(
            search_row, textvariable=self.count_var, font=FONT_UI, foreground=PICK_BLUE
        ).pack(side="right")

        holder = ttk.Frame(self)
        holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            holder,
            columns=("pick", "name"),
            show="headings",
            height=height,
            selectmode="none",
        )
        self.tree.heading("pick", text="选择")
        self.tree.heading("name", text="会话（好友备注名 / 昵称）")
        self.tree.column("pick", width=58, anchor="center", stretch=False)
        self.tree.column("name", width=300, anchor="w", stretch=True)
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.tag_configure("picked", background="#F1EFE8")
        self.tree.tag_configure("outside", foreground=OUTSIDE_AMBER)

        footer = ttk.Frame(self)
        footer.pack(fill="x", pady=(6, 0))
        ttk.Button(
            footer, text="全选当前结果", width=13, command=self.select_all_visible
        ).pack(side="left")
        ttk.Button(footer, text="清空已选", width=10, command=self.clear_selection).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(
            footer, text="点名字即勾选 / 取消", font=FONT_HINT, foreground=HINT_GREY
        ).pack(side="left", padx=(10, 0))

    # ------------------------------------------------------------------ 对外
    def set_data(self, fetched, selected) -> None:
        """重设候选池与已选（切账号 / 拉取完成后调用）。

        ``fetched`` 是本次抓到的会话名。``selected`` 里凡是 ``fetched`` 中没有的，
        都是老版本手填留下的数据 —— 它们已经没有来源了，但也不能悄悄丢掉
        （用户可能正靠它们发消息），所以照样列出来、标成琥珀色，
        让用户自己决定留还是删。
        """
        fetched = _unique_names(fetched)
        selected = _unique_names(selected)
        self._fetched = fetched
        self._fetched_set = set(fetched)
        self._pool = fetched + [
            name for name in selected if name not in self._fetched_set
        ]
        self._selected = selected

        # 换账号/重抓之后旧关键词很可能一个都匹配不上，留着只会让人以为列表空了
        self._rebuilding = True
        try:
            self.search_var.set("")
        finally:
            self._rebuilding = False
        self._rebuild()

    def set_selected(self, selected) -> None:
        """外部同步已选（模型被别处改过时）。与当前一致就不动，省一次重画。"""
        selected = _unique_names(selected)
        if selected == self._selected:
            return
        self._selected = selected
        self._rebuild()

    def get_selected(self) -> list:
        return list(self._selected)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def set_fetch_enabled(self, enabled: bool) -> None:
        self.btn_fetch.config(state="normal" if enabled else "disabled")

    def select_all_visible(self) -> None:
        """把当前筛选出来的全部勾上（已选的其他项保留）。"""
        changed = False
        for name in self._rows:
            if name not in self._selected:
                self._selected.append(name)
                changed = True
        if not changed:
            return
        for name in self._rows:
            self._refresh_row(name)
        self._refresh_count()
        self._notify()

    def clear_selection(self) -> None:
        if not self._selected:
            return
        self._selected = []
        self._rebuild()
        self._notify()

    # ------------------------------------------------------------------ 内部
    def _fetch(self) -> None:
        if self._on_fetch:
            self._on_fetch()

    def _notify(self) -> None:
        if self._on_change:
            self._on_change()

    def _rebuild(self) -> None:
        if self._rebuilding:
            return
        keyword = self.search_var.get().strip().lower()
        chosen = set(self._selected)
        self._rows = [n for n in self._pool if not keyword or keyword in n.lower()]

        self._rebuilding = True
        try:
            self.tree.delete(*self.tree.get_children())
            for index, name in enumerate(self._rows):
                self.tree.insert(
                    "",
                    "end",
                    iid=str(index),
                    values=(CHECKED if name in chosen else UNCHECKED, self._label(name)),
                    tags=self._tags(name, name in chosen),
                )
        finally:
            self._rebuilding = False
        self._refresh_count()

    def _tags(self, name: str, picked: bool) -> tuple:
        tags: list = []
        if name not in self._fetched_set:
            tags.append("outside")
        if picked:
            tags.append("picked")
        return tuple(tags)

    def _label(self, name: str) -> str:
        if name in self._fetched_set:
            return name
        return f"{name}   （不在会话列表里）"

    def _refresh_row(self, name: str) -> None:
        """只改这一行的勾选显示 —— 不重画整表，滚动位置才守得住。"""
        try:
            index = self._rows.index(name)
        except ValueError:
            self._rebuild()
            return
        picked = name in set(self._selected)
        self.tree.item(
            str(index),
            values=(CHECKED if picked else UNCHECKED, self._label(name)),
            tags=self._tags(name, picked),
        )

    def _refresh_count(self) -> None:
        total = len(self._pool)
        text = f"已选 {len(self._selected)} 个"
        if self._rows and len(self._rows) != total:
            text += f" · 筛出 {len(self._rows)}/{total}"
        self.count_var.set(text)

    def _on_click(self, event) -> None:
        if self._rebuilding:
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return  # 点在表头或空白处
        try:
            name = self._rows[int(row)]
        except (IndexError, ValueError):
            return
        self._toggle(name)

    def _toggle(self, name: str) -> None:
        if name in self._selected:
            self._selected.remove(name)
        else:
            self._selected.append(name)
        self._refresh_row(name)
        self._refresh_count()
        self._notify()


class ScrolledText(tk.Text):
    """带滚动条、默认只读的文本区。"""

    def __init__(self, master, height=8, readonly=True, mono=False, **kwargs) -> None:
        container = tk.Frame(master)
        super().__init__(
            container,
            height=height,
            wrap="word",
            font=FONT_MONO if mono else FONT_UI,
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=6,
            bg="#FFFFFF",
            **kwargs,
        )
        scroll = ttk.Scrollbar(container, orient="vertical", command=self.yview)
        self.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.pack(side="left", fill="both", expand=True)
        self.container = container
        if readonly:
            self.configure(state="disabled")

    def write_line(self, text: str) -> None:
        self.configure(state="normal")
        self.insert("end", text + "\n")
        self.see("end")
        self.configure(state="disabled")

    def set_text(self, text: str) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.insert("1.0", text)
        self.configure(state="disabled")
