"""启动命令推导：把「跑一轮任务」翻译成当前运行形态下可执行的 argv。

   源码运行： [python, <root>/main.py, task]
   exe 运行： [<exe>, task]                 （sys.frozen 为真时）

不写死解释器路径：源码下 sys.executable 就是 python，打包后就是 exe 自身。
调度器自身的入口同理（scheduler_argv），供「开机执行」包装脚本调用 run-if-due。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from app.scheduler import paths


@dataclass
class Launcher:
    exe: str
    frozen: bool
    root: Path

    @classmethod
    def detect(cls) -> "Launcher":
        """按当前进程的运行形态推导。"""
        return cls(exe=sys.executable, frozen=paths.FROZEN, root=paths.ROOT)

    @classmethod
    def for_exe(cls, exe_path: str) -> "Launcher":
        """显式指定一个 exe（例如想给已打包的程序注册任务）。"""
        resolved = Path(exe_path).expanduser().resolve()
        return cls(exe=str(resolved), frozen=True, root=resolved.parent)

    @classmethod
    def for_python(cls, python_path: str) -> "Launcher":
        """显式指定 python 解释器（源码形态）。"""
        return cls(
            exe=str(Path(python_path).expanduser()),
            frozen=False,
            root=paths.ROOT,
        )

    def task_argv(self) -> list:
        """执行一轮任务。"""
        if self.frozen:
            return [self.exe, "task"]
        return [self.exe, str(self.root / "main.py"), "task"]

    def scheduler_argv(self, *args: str) -> list:
        """调度器自身入口（如 run-if-due）。"""
        if self.frozen:
            return [self.exe, "scheduler", *args]
        return [self.exe, str(self.root / "main.py"), "scheduler", *args]
