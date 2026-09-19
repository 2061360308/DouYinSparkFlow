# 仓库结构

```
DouYinSparkFlow/
├── main.py                     # 主入口：python main.py [task|fc]
├── core/
│   ├── douyin_im.py            # 抖音 IM 核心：登录判定、会话扫描、发消息
│   ├── browser.py              # 浏览器启动（cloakbrowser / playwright）
│   └── tasks.py                # 任务编排：跑一轮续火花
├── utils/
│   ├── config.py               # 环境变量读取（.env → 配置字典）
│   ├── logger.py               # 日志器
│   └── export_github_env.py    # Action 场景：把 vars/secrets 注入进程环境
├── configTool/                 # 本地配置生成器（tkinter 桌面工具）
│   ├── main.py                 # 主窗口（从根入口 run_configtool.py 启动）
│   ├── browser_login.py        # 登录会话工作线程（借 core/douyin_im）
│   ├── login_dialog.py         # 登录窗口
│   ├── conversation_dialog.py  # 拉取会话列表窗口
│   ├── models.py               # 配置项定义（键名、范围、默认值）
│   ├── env_store.py            # 读写 .env
│   ├── profile_store.py        # 读写 profiles.json（账号 ↔ 浏览器目录对照）
│   ├── local_settings.py       # 工具私有设置（local.json）
│   ├── tunnel.py               # gost 隧道进程生命周期
│   └── paths.py                # 路径解析（.env 落仓库根，工具数据留 configTool/）
├── tools/
│   └── record_har.py           # HAR 录制工具（离线分析流量）
├── tests/                      # 单元测试
├── docker/                     # 容器入口脚本（entrypoint / run-task）
├── docker-compose.yml          # 两容器编排（任务 + gost 代理）
├── aliyun-fc-ros-template.yaml # 云函数 ROS 模板（两函数）
├── run_configtool.py           # configTool 的启动入口（唯一）
└── docs/                       # 本套文档（docsify）
```

## 两个入口

| 入口 | 用途 |
| --- | --- |
| `python main.py task` | 跑一轮续火花任务 |
| `python main.py fc` | 云函数模式，起 HTTP Server 等定时触发器 |
| `python run_configtool.py` | 启动本地配置生成器 |

## 核心约定

- `.env` 是主程序真正读的配置，`utils/config.py` 负责解析
- 配置键名、范围、默认值以 `configTool/models.py` 为准（与网页版、`.env.example` 保持一致）
- 登录态判定、会话扫描逻辑**只在** `core/douyin_im.py` 里有一份，configTool 直接复用，不自己镜像
