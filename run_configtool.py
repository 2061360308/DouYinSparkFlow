"""DouYinSparkFlow 本地配置生成器的入口。

放在仓库根是为了让 `import core.douyin_im` 直接可用，同时给 PyInstaller 一个
能静态分析出全部依赖的入口。

用法：python run_configtool.py
"""

from configTool.main import main

if __name__ == "__main__":
    main()
