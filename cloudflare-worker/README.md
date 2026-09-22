# Cloudflare Workers 部署说明

本目录是 DouYinSparkFlow 的 **Cloudflare Workers 版本**，基于 Cloudflare [Browser Rendering（Browser Run）](https://developers.cloudflare.com/browser-rendering/) + `@cloudflare/puppeteer`，将原项目的 Playwright 续火逻辑移植到 Cloudflare 边缘运行时，无需自有服务器，由 **Cron Triggers 定时执行**。

## 与原版本的对应关系

| 原项目（Playwright / Docker / GH Actions） | 本版本（Workers） |
| --- | --- |
| `main.py` + `core/tasks.py` | `src/worker.js` + `src/core/tasks.js` |
| `utils/config.py`（.env 环境变量） | `src/utils/config.js`（wrangler vars + secrets，变量名一致） |
| `utils/hitokoto.py` 一言 | `src/utils/hitokoto.js` |
| `core/msg_builder.py` 消息模板 | `src/core/msg_builder.js`（`[API]` 占位符语义不变） |
| Docker `CRON_HOUR/CRON_MINUTE` 定时 | `wrangler.toml` 的 `[triggers] crons` |
| GH Actions `environment: user-data` Secrets | `wrangler secret put` |

## 前置要求

1. 一个 Cloudflare 账号（免费计划可用，免费计划含**每天 10 分钟**浏览器时长，续火任务单次约 1–3 分钟，每天一次绰绰有余）
2. Node.js 18+ 与 npm
3. 抖音网页版的 Cookie（获取方式见[原项目配置文档](../docs/配置生成器使用.md)）

## 部署步骤

### 1. 安装依赖

```bash
cd cloudflare-worker
npm install
```

### 2. 登录 Wrangler

```bash
npx wrangler login
```

### 3. 配置任务与 Cookie（Secrets，不要明文写在配置里）

```bash
# 任务列表，JSON 数组（单行），格式与原项目 TASKS 完全一致
npx wrangler secret put TASKS
# 内容示例：
# [{"username":"账号1","unique_id":"12345678905","targets":["好友A","好友B"]}]

# 每个账号一份 Cookie，变量名为 COOKIES_<UNIQUE_ID大写>
npx wrangler secret put COOKIES_12345678905
# 内容示例（浏览器导出的 Cookie JSON 数组）：
# [{"name":"sessionid","value":"xxx","domain":".douyin.com","path":"/"},{"name":"ttwid","value":"yyy","domain":".douyin.com","path":"/"}]

# （可选）手动触发接口 /run 的鉴权 Token
npx wrangler secret put RUN_TOKEN
```

多账号就多放几组 `COOKIES_<UNIQUE_ID>`，`unique_id` 必须与 `TASKS` 中一致。

### 4. （可选）修改定时与消息模板

编辑 `wrangler.toml`：

- `[triggers] crons`：默认 `0 1 * * *`（UTC），即北京时间每天 09:00。**注意 cron 用 UTC 时间**，北京时间 = UTC + 8。
- `[vars] MESSAGE_TEMPLATE`：消息模板，`[API]` 会被替换为一言内容；`\n` 表示换行。
- `[vars] HITOKOTO_TYPES`：一言类型，可选值见 `.env.example`。

### 5. 部署

```bash
npx wrangler deploy
```

部署成功后输出 Worker 的 URL。Cron Triggers 随部署自动生效。

### 6. 验证

```bash
# 查看配置解析结果（不会启动浏览器，验证 TASKS/COOKIES 是否被正确读取）
curl https://douyinsparkflow-worker.<你的子域>.workers.dev/status

# 手动触发一次续火（配置了 RUN_TOKEN 时需带 ?token=xxx 或 X-Run-Token 头）
curl "https://douyinsparkflow-worker.<你的子域>.workers.dev/run"

# 实时查看日志
npx wrangler tail
```

首次 `/run` 前请确认 Cloudflare 控制台中该 Worker 已开启 **Browser Rendering** 绑定（`wrangler.toml` 的 `[browser] binding = "BROWSER"` 部署时自动创建；若控制台提示需订阅 Browser Rendering，按页面指引免费开通即可）。

## 本地开发

```bash
cp .dev.vars.example .dev.vars   # 填入 TASKS / COOKIES_*
npx wrangler dev
```

> 注意：本地 `wrangler dev` 没有真实浏览器绑定，`/status` 可正常验证配置，但续火任务需要 deploy 到 Cloudflare 后通过 `/run` 或 Cron 执行。

## 常见问题

- **免费计划额度够吗？** 单账号任务通常 1–3 分钟浏览器时长，免费计划的每天 10 分钟足够每天一次的续火；多账号/多目标请注意累计时长。
- **抖音会检测 Cloudflare 的浏览器吗？** Browser Rendering 的请求会被识别为自动化浏览器，与原项目 GitHub Actions 方式存在相同的被风控风险（原 README 也提到过），请自行评估；消息发送可靠性依赖抖音网页版页面结构，页面改版导致选择器失效时可参考上游仓库更新。
- ** Workers 有执行时长限制吗？** Cron 触发的 Worker 最长可运行 15 分钟，浏览器会话 keep_alive 上限 10 分钟，本工具已按此配置；任务超时会以日志形式报错。
- **Cookie 什么时候会失效？** 与原版本一致，sessionid 过期后需要重新从浏览器导出并更新对应 `COOKIES_*` Secret。
