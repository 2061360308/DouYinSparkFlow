"""滚轮保护的单测：滚轮不得改数值框/下拉框的值，但仍要能滚表单。

这些测试要真的建 Tk 窗口；没有显示时整类 skip。
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
        """不保护时滚轮会改值（前提）；加上保护后必须不变。

        某些 Tk 版本没有这个类绑定，那就 skip —— 是不需要保护，不是保护写错了。
        """
        box, var = self._spinbox(self.root, 5)
        box.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        if var.get() == 5:
            self.skipTest("这个 Tk 版本的 Spinbox 没有滚轮改值行为")

        box2, var2 = self._spinbox(self.root, 5)
        widgets.disable_wheel_change(box2)
        box2.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertEqual(var2.get(), 5, "滚轮把数值改了 —— 保护没生效")

    def test_wheel_is_forwarded_to_the_scroller(self):
        """值不变，但滚轮要转交给所属滚动容器，不能就此失效。"""
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
