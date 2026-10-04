"""跨平台运行锁：避免「常驻定时」与「开机执行」同时跑同一个任务。

用 O_CREAT|O_EXCL 原子创建锁文件。若锁文件已存在但很久没动（比如上次进程被
强杀），按陈旧锁处理，删掉重试，免得永久卡死。
"""

from __future__ import annotations

import os
import time
from pathlib import Path


class LockBusy(Exception):
    """已有任务在运行。"""


class RunLock:
    STALE_SECONDS = 6 * 3600

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._acquired = False

    def _try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
        finally:
            os.close(fd)
        self._acquired = True
        return True

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        return age > self.STALE_SECONDS

    def __enter__(self) -> "RunLock":
        if self._try_acquire():
            return self
        if self._is_stale():
            try:
                self.path.unlink()
            except OSError:
                pass
            if self._try_acquire():
                return self
        raise LockBusy("已有任务在运行")

    def __exit__(self, *_exc) -> None:
        if self._acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
            self._acquired = False
