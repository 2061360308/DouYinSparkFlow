# GitHub Action 部署（过时）

> ⚠️ **此方式已过时，不推荐。** 保留文档是为了有需要的老用户参考。

## 为什么过时

1. **GitHub 严格的 Action 审查**：公共仓库的定时任务容易被限制甚至禁用。
2. **出口 IP 每次运行都变**：抖音校验登录 Cookie 的出口 IP，IP 漂移会导致登录态掉，需要额外配固定代理。

## 如果要继续用（私有仓库方案）

要绕开 GitHub 对公共仓库 Action 的严格检查，需要把代码搬到私有仓库：

1. 把仓库 clone 到本地
2. 在 GitHub 上新建一个**私有**仓库
3. 把代码推送到私有仓库
4. 在私有仓库里启用 Action、配置定时任务

私有仓库的 Action 限制相对宽松，但**出口 IP 漂移的问题依然存在**，仍需自备固定代理。

## 关于出口 IP

Action 每次运行的 runner 不同，出口 IP 每次都变。抖音登录态绑定出口 IP，所以：

1. 需要一个固定出口的 HTTP 代理
2. 把代理地址写进 `.env` 的 `PROXY_ADDRESS`
3. 抓 Cookie 时也要走同一个代理出口

详细原理见 [Cookie 与出口 IP](guide/02-cookie与出口IP.md)。

## 历史部署流程（供参考）

老用户如需按原流程配置，可参考仓库根目录的历史 `docs/Action部署说明.md`（如已删除则看 git 历史）。核心步骤：

1. Fork 仓库
2. 创建名为 `user-data` 的 Environment
3. 在该 Environment 下配置 Variables 和 Secrets（逐条粘贴配置生成器产出）
4. 手动触发一次验证

> 如果你能自备服务器，请直接改用 [服务器 Docker](docker.md)，省去代理和私有仓库的折腾。
