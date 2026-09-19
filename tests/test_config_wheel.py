"""滚轮保护的单测（需要可用显示；没有就整类 skip）。

钉住的问题是真实的：Windows 上 ttk 的 Spinbox / Combobox 自带「滚轮改值」的类绑定，
指针路过就把数字改了 —— 而 main.py 里每个数值框都挂了 `trace_add(write)`，
改完立刻自动写进 .env。用户只是滚了一下页面，配置就变了，且毫无提示。

两条不变量：
  · `disable_wheel_change` 之后，滚轮**不得**改值；
  · 但滚轮也不能就此失效 —— 要把它转交给所属滚动容器，页面照常滚。

⚠️ 这些测试要真的建 Tk 窗口。CI/容器里没有显示时跳过，不算失败。
"""

import unittest

import tkinter as tk
from tkinter import ttk

from configTool import widgets


def _tk_available() -> bool:
    try:
        root = tk.Tk()
    except Exception:
        return False
    root.destroy()
    return True


TK_OK = _tk_available()


class _FakeScroller(ttk.Frame):
    """冒充 main.ScrollFrame：`_find_scroller` 就是靠 `_on_wheel` 认人的。"""

    def __init__(self, master):
        super().__init__(master)
        self.wheel_deltas = []

    def _on_wheel(self, event):
        self.wheel_deltas.append(getattr(event, "delta", None))


@unittest.skipUnless(TK_OK, "没有可用的 Tk 显示")
class WheelGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        # 窗口不需要可见，但要真实存在，event_generate 才会走绑定链
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _spinbox(self, master, value: int = 5) -> tuple:
        var = tk.IntVar(value=value)
        box = ttk.Spinbox(master, from_=0, to=10, textvariable=var)
        box.pack()
        self.root.update()
        return box, var

    def test_baseline_wheel_does_change_value(self):
        """先钉住前提：不保护的话，滚轮确实会改值。

        这条**不是为了失败**：万一某个 Tk 版本没有这个类绑定，它会 skip ——
        那也说明我们不需要这层保护，而不是保护写错了。
        """
        box, var = self._spinbox(self.root, 5)
        box.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        if var.get() == 5:
            self.skipTest("这个 Tk 版本的 Spinbox 没有滚轮改值行为")

        # 带保护时必须纹丝不动
        box2, var2 = self._spinbox(self.root, 5)
        widgets.disable_wheel_change(box2)
        box2.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertEqual(var2.get(), 5, "滚轮把数值改了 —— 保护没生效")

    def test_wheel_is_forwarded_to_the_scroller(self):
        """★ 值不变，但滚轮不能就此失效：要转交给所属滚动容器。"""
        scroller = _FakeScroller(self.root)
        scroller.pack()
        box, var = self._spinbox(scroller, 5)

        widgets.disable_wheel_change(box)
        box.event_generate("<MouseWheel>", delta=-120)
        self.root.update()

        self.assertEqual(var.get(), 5, "值被改了")
        self.assertEqual(scroller.wheel_deltas, [-120], "滚轮没有转交给滚动容器")

    def test_combobox_is_covered_too(self):
        """下拉框同一类问题，同一套处理。"""
        scroller = _FakeScroller(self.root)
        scroller.pack()
        var = tk.StringVar(value="B")
        combo = ttk.Combobox(scroller, textvariable=var, values=("A", "B", "C"))
        combo.pack()
        self.root.update()

        widgets.harden_wheel(self.root)
        combo.event_generate("<MouseWheel>", delta=-120)
        self.root.update()

        self.assertEqual(var.get(), "B", "滚轮把下拉框的选项改了")

    def test_harden_wheel_walks_the_whole_tree(self):
        """遍历式保护：嵌套在几层里的框也要覆盖到，且返回值要准。"""
        outer = ttk.Frame(self.root)
        outer.pack()
        inner = ttk.Frame(outer)
        inner.pack()
        ttk.Spinbox(inner, from_=0, to=10).pack()
        ttk.Combobox(inner, values=("A", "B")).pack()
        ttk.Entry(inner).pack()          # 不受影响，不该被算进去
        ttk.Frame(inner).pack()
        self.root.update()

        self.assertEqual(widgets.harden_wheel(self.root), 2)


if __name__ == "__main__":
    unittest.main()
