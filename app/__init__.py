"""DouYinSparkFlow 本地可视化工具（app）。

做成包是为了由仓库根的入口启动：这样 `import core.douyin_im` 直接可用（仓库根在
sys.path 上），打包时也能被静态分析。

启动方式：在仓库根执行 `python main.py app`。

结构：
    config/     配置与持久化（models / env_store / settings / profile_store）
    browser/    浏览器相关（worker / tunnel / sessions）
    web/        传输与编排（bridge / service / host / window / ui）
    scheduler/  本机定时任务（常驻定时 / 开机执行 / 生成配置）
"""

from __future__ import annotations
