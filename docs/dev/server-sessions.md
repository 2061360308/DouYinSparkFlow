# 服务器登录状态持久化

AI 陪聊和每日任务默认按账号复用浏览器登录状态。保存内容包括 Cookie、localStorage 和 IndexedDB；不修改网站签发的到期时间。

默认目录为 `app/sessions/`，可通过 `DOUYIN_SESSION_DIR` 指定。容器部署需要把这个目录挂载到持久卷，并保留 `.env` 中的账号和固定 fingerprint。状态文件不入 Git；Linux 文件权限为 600，目录为 700。备份时将它们视为登录凭据。

陪聊成功连接后保存状态，运行期间每 5 分钟保存一次，在正常停止时再保存。续火花任务也复用相同状态。每次登录时，新的 Cookie 会使旧快照失效，并由扫码登录流程保存新快照。并行任务持有旧快照时不能覆盖已经更新的状态。

登录状态由服务器上的网页实际验证，不能根据 Cookie 中的 expires 判定仍然有效。网页要求重新登录时仍需扫码；持久化无法保证固定有效天数，也不能恢复已经被抖音撤销的登录。

从本地登录后迁移到服务器，应同时传输 `.env` 中该账号的 Cookie 和对应状态文件；不要用旧 Cookie 覆盖状态文件中的更新凭据。尽量在固定服务器上运行，避免在多台机器上反复登录同一个账号。

复用范围遵循 Playwright 的 storage_state：Cookie、localStorage 和 IndexedDB；不包含完整浏览器用户目录和 sessionStorage。参考 [Playwright 登录状态文档](https://playwright.dev/python/docs/auth)。

## 登录状态检测

项目根目录执行 `python scripts/check_cookies.py`，只读检查所有已配置账号的登录状态，不发送聊天消息，不更新 Cookie，不重启陪聊。

结果保存在 `cookie-health.json`，日志保存在 `logs/cookie-health.log`，最多保留 3 个 1 MB 的轮转文件。检测状态分为 valid（登录且会话就绪）、invalid（需要重新登录）、unknown（网络或页面异常）。首次发现登录要求时会等待 5 秒再独立验证一次。

记录包括 Cookie 标注到期时间、最后有效时间和首次发现失效时间；实际失效只能确定在最后有效与首次失效之间。unknown 不记为失效。日志与状态不包含 Cookie 值和 API Key。

如需每天持续检测，可使用 `deploy/systemd/` 中的服务和定时器样例。将项目路径、Python 路径、运行账号和浏览器路径替换为自己的环境，再安装到 `/etc/systemd/system/`，执行 `systemctl daemon-reload` 和 `systemctl enable --now douyin-cookie-health.timer`。样例每小时第 13、43 分钟检查，时区为 Asia/Shanghai；不要为检测开放公网管理接口。

AI 陪聊连续 3 轮无法读取会话页面或选中任一会话时会停止连接并报告异常，交由进程管理器重启；避免进程存在但实际不读取消息。被撤销的登录态不会通过重启恢复。
