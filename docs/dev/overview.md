# 仓库结构

```
DouYinSparkFlow/
├── main.py                     # 主入口：python main.py [task|fc|app]
├── core/
│   ├── douyin_im.py            # 抖音 IM 核心：登录判定、会话扫描、发消息
│   ├── fc_server.py            # 云函数(FC)模式：HTTP Server 等定时触发器
│   ├── browser.py              # 浏览器启动（cloakbrowser / playwright）
│   ├── msg_builder.py          # 消息模板 / 一言生成
│   ├── notify.py               # 任务完成后的消息通知（Bark/Telegram/企业微信…）
│   └── tasks.py                # 任务编排：跑一轮续火花
├── utils/
│   ├── config.py               # 环境变量读取（.env → 配置字典）
│   ├── logger.py               # 日志器
│   └── export_github_env.py    # Action 场景：把 vars/secrets 注入进程环境
├── app/                        # 本地可视化工具（Vite 前端 + 自带 Chromium 当 webview）
│   ├── web/                    # 界面层
│   │   ├── host.py             # 浏览器窗口壳（playwright 启动自带 Chromium）
│   │   ├── bridge.py           # 桥：页面 ↔ Python 双向通信（window.$py / __pyOn）
│   │   ├── service.py          # 业务封装（复用 core/ 与下方数据模块）
│   │   ├── account_ops.py      # 浏览器账号会话（添加账号 / 刷新登录 / 拉会话）
│   │   ├── ui/                 # Vite 前端源码（worker 里构建，产物不入库）
│   │   └── dist/               # vite build 单文件产物（CI 里生成）
│   ├── scheduler/              # 本机定时任务（常驻定时 / 开机执行，schtasks / cron）
│   ├── browser_login.py        # 登录会话工作线程（借 core/douyin_im）
│   ├── models.py               # 配置项定义（键名、范围、默认值）
│   ├── notify_spec.py          # 通知方式规格（类型/字段，前后端共用）
│   ├── env_store.py            # 读写 .env
│   ├── profile_store.py        # 读写 profiles.json（账号 ↔ 浏览器目录对照）
│   ├── local_settings.py       # 工具私有设置（local.json）
│   ├── tunnel.py               # gost 隧道进程生命周期
│   └── paths.py                # 路径解析（.env 落仓库根，工具数据留 app/）
├── tools/
│   ├── record_har.py           # HAR 录制工具（离线分析流量）
│   └── packaging/linux/        # Linux deb 打包脚本与 deb 元数据（构建时用）
├── tests/                      # 单元测试
├── docker/                     # 容器入口脚本（entrypoint / run-task）
├── docker-compose.yml          # 两容器编排（任务 + gost 代理）
├── aliyun-fc-ros-template.yaml # 云函数 ROS 模板（两函数）
└── docs/                       # 本套文档（docsify）
```

## 四个入口

| 入口 | 用途 |
| --- | --- |
| `python main.py task` | 跑一轮续火花任务 |
| `python main.py fc` | 云函数模式，起 HTTP Server 等定时触发器 |
| `python main.py app` | 启动本地可视化工具（配置 / 账户 / 定时任务） |
| `python main.py scheduler ...` | 注册/卸载本机系统定时任务（`app/scheduler/`） |

## 核心约定

- `.env` 是主程序真正读的配置，`utils/config.py` 负责解析
- 配置键名、范围、默认值以 `app/models.py` 为准（与 `.env.example` 保持一致）
- 登录态判定、会话扫描逻辑**只在** `core/douyin_im.py` 里有一份，app 直接复用，不自己镜像
