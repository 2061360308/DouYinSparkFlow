# 工具与测试

## 测试

项目用 Python 标准库 `unittest`，不需要额外依赖。

```bash
python -m unittest discover -s tests -t .
```

测试文件：

| 文件 | 覆盖 |
| --- | --- |
| `test_douyin_im.py` | 登录判定、会话枚举、匹配、消息拆分等核心逻辑 |
| `test_douyin_im_har.py` | 基于 HAR 录制的离线回归 |
| `test_config_tool.py` | 配置键名、默认值、CRLF 归一 |
| `test_config_login_flow.py` | 登录流程的门禁 / 刷新约束 |
| `test_web_bridge.py` | app 网页桥（$py）的纯逻辑 |
| `test_web_host_integration.py` | 真实浏览器跑网页界面：读配置、保存回 .env |
| `test_scheduler.py` | 本机定时任务：命令拼装、cron 行增删、run-if-due 判定、模式切换 |
| `test_logger.py` | 日志器配置 |

部分 GUI 相关测试需要可用的显示环境，没有时整类跳过（不算失败）。

## HAR 录制工具

`tools/record_har.py` 用来打开抖音聊天页录一段流量，供离线分析（比如排查接口结构变化）。

```bash
python tools/record_har.py                 # 第一个账号，录 300 秒
python tools/record_har.py --seconds 60    # 录 60 秒
python tools/record_har.py --account 12345678901
python tools/record_har.py --out har_logs/my.har
```

产出在 `har_logs/` 下，文件名 `<用户名>_<时间戳>.har`。

> ⚠️ HAR 里含登录凭据（Cookie）和好友资料，**绝不提交**。`har_logs/` 已在 `.gitignore`。

## 本地定时调度（app/scheduler/）

把「跑一轮任务」注册成本机的系统任务。三种模式：

| 模式 | 场景 | 注册方式 | 隧道 |
| --- | --- | --- | --- |
| 常驻定时 | 挂机宝 / 服务器，长期开机 | Windows `schtasks /SC DAILY`（动作是 `wscript` 跑隐藏 VBS）；Linux cron `M H * * *` | 不需要 |
| 开机执行 | 家用电脑，不常开 | Windows 放进「启动」文件夹的隐藏 VBS（免管理员）；Linux cron `@reboot` + run-if-due | 不需要 |
| 生成配置 | 本机只产 .env，拿去 Docker 服务器跑 | 不注册 | 需要 |

命令（app 界面里通过侧栏头部的「运行模式」切换，等价于下面的 CLI）：

```bash
python -m app.scheduler status                 # 查看当前模式与注册状态
python -m app.scheduler install --mode scheduled --time 09:00
python -m app.scheduler install --mode boot
python -m app.scheduler uninstall
python main.py scheduler status                # 也可以走 main.py 分派
```

要点：

- **命令按运行形态推导**：源码运行注册的是 `python main.py task`；打包成 exe 后是 `<exe> task`。不写死解释器。
- **Windows 用隐藏 VBS**：定时模式生成 `.scheduler/run_task.vbs`，schtasks 的动作是 `wscript.exe "<vbs>"`；开机模式把 `<name>.vbs` 放进用户「启动」文件夹。都隐藏运行，**执行时没有控制台窗口**。
- **Linux 用 `.sh` + cron**：`run_task.sh` / `run_if_due.sh` 放 `.scheduler/`，内含 `cd` 到项目根并重定向日志。
- **开机执行**的入口是 `run-if-due`：当天已成功跑过就跳过，否则补跑；**只有退出码 0 才算成功**，失败留待下次开机重试。
- 状态与安装记录在 `.scheduler/`（`state.json` / `install.json`），与 `local.json` / `window.json` 同在程序目录，已加入 `.gitignore`。
- 测试或不想真注册时，设 `SCHEDULER_BACKEND=noop`（不碰 schtasks/crontab）、`APP_SCHEDULE_AUTOREGISTER=0`（首次启动不自动注册）。
