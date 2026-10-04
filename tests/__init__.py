"""测试包：默认关闭真实系统任务注册，避免测试污染开发机。

    SCHEDULER_BACKEND=noop                     调度器不碰 schtasks/crontab
    APP_SCHEDULE_AUTOREGISTER=0         首次启动不自动注册任务

注意：unittest 以不同 top-level 发现测试时本文件未必被导入，所以个别 web 测试
模块也会各自 setdefault 一次（见 test_web_*.py 顶部）。
"""

import os

os.environ.setdefault("SCHEDULER_BACKEND", "noop")
os.environ.setdefault("APP_SCHEDULE_AUTOREGISTER", "0")
