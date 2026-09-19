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
| `test_config_wheel.py` | 数值框滚轮保护 |
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
