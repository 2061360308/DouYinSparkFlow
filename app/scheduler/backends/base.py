"""系统任务后端的公共接口。"""

from __future__ import annotations


class Backend:
    """把「注册/卸载/查询」一个系统任务抽象出来。"""

    name = "base"

    def available(self) -> tuple:
        """返回 (是否可用, 不可用原因)。"""
        return True, ""

    def install(self, *, name: str, mode: str, run_time: str, launcher) -> None:
        """注册任务。mode 为 'scheduled'（每天定点）或 'boot'（开机/登录）。

        launcher 是 Launcher（提供 task_argv / scheduler_argv），由后端决定
        启动器落盘位置与形式（Windows 用隐藏 VBS，Linux 用 sh）。
        """
        raise NotImplementedError

    def uninstall(self, *, name: str) -> bool:
        """卸载任务，返回是否真的删掉了。任务不存在也当成功（幂等）。"""
        raise NotImplementedError

    def status(self, *, name: str, mode: str = "") -> dict:
        """返回 {'installed': bool, 'detail': str}。mode 用于区分后端机制。"""
        raise NotImplementedError
