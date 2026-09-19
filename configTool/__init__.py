"""DouYinSparkFlow 本地配置生成器（configTool）。

为什么是一个包，而不是一堆平铺模块：
  - 由仓库根的 `run_configtool.py` 启动时，仓库根天然就在 sys.path[0] 上，
    于是本包可以直接 `import core.douyin_im` / `import utils.config` ——
    会话扫描、登录态判定复用主程序那一份实现，**不需要任何 sys.path 引导**；
  - PyInstaller 从根入口打包时能静态分析出全部依赖，不用逐个 --hidden-import。

启动方式只有一种：在仓库根执行 `python run_configtool.py`
（直接跑 `python configTool/main.py` 会因缺少包上下文而失败，已刻意不支持）。
"""
