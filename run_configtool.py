"""DouYinSparkFlow 本地配置生成器的入口（唯一启动点）。

为什么入口放在仓库根，而不是 configTool/ 里面：
  - 从这里启动时，仓库根天然就是 sys.path[0]，于是 configTool 可以直接
    `import core.douyin_im` / `import utils.config` —— 「抖音页面怎么点、
    怎么滚、怎么判登录」复用主程序那一份实现，不需要往 sys.path 里塞路径。
  - PyInstaller 也从这里打包，静态分析就能把 configTool 与 core.douyin_im
    一起收进包里。

用法：
    python run_configtool.py        # 源码方式
发布版直接运行打包好的 exe（见 .github/workflows/build-configtool.yml）。
"""

from configTool.main import main

if __name__ == "__main__":
    main()
