# DouYin Spark Flow

## 多用户火花控制台（功能分支）

本仓库新增了一个独立的 FastAPI 控制台：受邀用户可加密保存自己的抖音 Cookie，设置目标好友、北京时间、发送内容并查看脱敏执行结果；管理员可以创建或停用用户、查看、暂停和删除所有任务。

- Cookie 使用 AES-256-GCM 加密，密码使用 Argon2id；页面不回显 Cookie。
- SQLite 使用 WAL，Worker 全局单并发，同一次计划只执行一次，失败不自动重发。
- Docker Compose 仅绑定 `127.0.0.1:8899`，使用独立网络、数据卷和资源限制，不接入既有业务容器网络。
- 正式给朋友使用前必须配置可信域名和 HTTPS。不得直接开放 8899 端口或通过明文 HTTP 传递账号凭据。
- 部署前要求根分区至少剩余 5 GiB，并确认系统时间误差不超过 5 秒。

完整部署、备份、导入和回滚步骤见 [控制台运维手册](docs/console-operations.md)。本功能仍属于浏览器自动化，页面变化和平台风控可能导致发送失败，使用者应遵守平台规则。

![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python)
![Playwright](https://img.shields.io/badge/Playwright-%E2%9C%94-green?logo=playwright)
![chrome-headless-shell](https://img.shields.io/badge/chrome--headless--shell-%E2%9C%94-brightgreen?logo=googlechrome)

## 🎉 2026 正月限定（除夕至正月十五）

除夕当天到正月十五期间，可开启祝福模式，每日向好友发送与当日相关的祝福语。

### 启用方法

拉取最新代码后，将 `config.json` 中 `happyNewYear` 下的 `enabled` 设为 `true`。

`happyNewYear.messageTemplate` 为正月祝福模板，支持以下占位符：

1. `[API]`：祝福语
2. `[data]`：公历日期，例如 2026年02月16日
3. `[data_lunar]`：农历日期，例如 农历除夕、正月初一、正月初二 等

## 交流讨论

已开放讨论区，有疑问或展示相关成果，发布话题需求的可以加入讨论

[跳转讨论区](https://github.com/2061360308/DouYinSparkFlow/discussions)

## 📌 简单介绍

**抖音火花自动续火脚本**一款轻量实用的抖音互动脚本，可自动为你和抖音好友续火花，无需手动操作。

✅ 支持 GitHub Actions 自动运行（开箱即用的 Workflow 配置）

✅ 也可部署至自有服务器，灵活适配个人使用场景

### 特性

- [x] 多用户,同时批量支持多个账户
- [x] 多目标,一个账户支持多个续火花目标
- [x] 一言支持,更丰富的消息文本

使用`PlayWright`以及`chrome-headless-shell`自动化操作[抖音创作者中心](https://creator.douyin.com/)，进行定时发送抖音消息来续火花

## 🚀 使用方法

### 1. 克隆项目到本地，并完成环境配置

```shell
pip install -r requirements.txt
cp usersData.example.json usersData.json
```

### 2. 运行main.py

首次运行`python main.py`时，会自动下载需要的测试浏览器，默认从Mozilla的镜像站下载，需要保证网络通畅。

![main.py运行截图](docs/images/屏幕截图%202026-02-14%20223607.png)

### 3. 登录用户

运行main.py后，会弹出可选择的项目，这时选择添加用户登录，你可以选择添加多个用户。具体操作方式根据提示在弹出窗口扫码登录抖音创作者中心即可，登录成功后你需要根据提示查看对应联系人的名称，并在控制台输入。

### 4. 更改配置

你可以选择更改config.json中的配置，目前proxyAddress的代理设置还没有实现。其他项目解释如下：

|名称|作用解释|期望值|
|-----|-----|-----|
|multiTask|是否启用多任务，登录多个账户后生效，启用后同时操作多个账户的任务加快执行速度|`true` `false`|
|taskCount|最大同时操作的账户数目，需要先启用multiTask|int，默认`5`|
|messageTemplate|发送消息的模板，可以从抖音聊天框编辑好后直接复制过来，这样可以拿到简单表情的代码，例如`[盖瑞]`|使用`[API]`引用每日一言内容 默认值为： `[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——\n[API]`|
|hitokotoTypes|每日一言消息允许的类型|可以留空使用所有类型`[]`,全部可选类型的列表为：`["动画","漫画","游戏","文学","原创","来自网络","影视","诗词","哲学","抖机灵","其他"]`|

### 5. 测试运行

再次运行main.py之后选择`3.本地运行任务`,查看是否能够正常执行任务

### 6. Github Acion部署

项目可以部署到Github Action每日定时触发，在测试完毕后，你需要将本地代码推送到自己的Github仓库，你也可以选择直接克隆本仓库后续将config.json同步即可（如果你更改了设置的话）。本地通过usersData.json存储已经登录的账户凭证，为了防止信息泄露，Action不能像本地那样从明文读取这个配置，也不要将这个文件上传到Github，正确做法是将内容存放到`secrets`中

> 方法: 在你的Github仓库下操作，选择settings->Environments，在下面新建一个`user-data`环境，继续在这个`user-data`环境的Environment secrets添加名为`wz_data`的项目

![创建`user-data`环境图](docs/images/屏幕截图%202026-02-14%20224915.png)

关于这个配置的内容可以再次运行main.py,选择`2. 获取Github Action配置`将对应输出内容填入`wz_data`的值即可

![填写配置内容图](docs/images/屏幕截图%202026-02-14%20224951.png)

### 7. （可选）手动触发Action进行测试

仓库的工作流中添加了`workflow_dispatch`以便允许进行手动触发，在初次配置完成后可以通过手动触发Action来进行验证。

![手动测试](docs/images/屏幕截图%202026-02-14%20224614.png)

## 💬 问题解答

1. 首次**克隆仓库**后启用Action

    **解答：**

    克隆后Github Action 默认在新仓库中是关闭的。你需要在克隆仓库后，手动进入你的 Github 仓库页面，依次点击 `Actions` 选项卡，首次进入会看到“启用工作流”或“Enable workflows”按钮，点击即可激活仓库中的 Action 工作流。

    启用后，工作流会根据 `.github/workflows` 目录下的配置自动运行。你可以通过手动触发（workflow_dispatch）或等待定时任务自动执行。

    > 注意：首次启用后建议手动运行一次，确保配置无误。

2. 运行一段时间后Github提示仓库太久没有新活动，定时Action被禁用

    > 通常提示：Scheduled workflows disabled
To reduce unnecessary workflow runs, scheduled workflows have been disabled in this repository because it has been more than 60 days since the last commit.

    ![Scheduled workflows disabled
To reduce unnecessary workflow runs, scheduled workflows have been disabled in this repository because it has been more than 60 days since the last commit.](docs/images/image.png)

    **解答：**

    这是 Github 的自动保护机制：如果仓库 60 天内没有任何提交或活动，所有定时（schedule）类型的 Action 会被自动禁用，防止资源浪费。

    遇到这种情况，只需在仓库内进行一次代码提交（如修改 README 或随便提交一个空白更改），然后重新进入 Actions 页面，点击提示条上的“Enable workflow”或“启用工作流”按钮，即可恢复定时任务。

    恢复后，定时 Action 会重新按照 workflow 文件的 schedule 设定自动运行。

    建议：如果你长期需要定时任务，定期（如每月）做一次无关紧要的提交，防止被自动禁用。

    > 补充，我在仓库中尝试引入了`liskin/gh-workflow-keepalive`，理论上在此之后复刻仓库的或者进行同步后的仓库不需要再手动保活，具体详见action的`workflow-keepalive` Job

## ⚠️ 免责声明

1. 本项目为**开源学习用途**，仅用于技术研究和个人自用，严禁用于商业用途、恶意刷量或违反抖音平台规则的行为。
2. 使用本脚本产生的一切风险（包括但不限于抖音账号限流、封禁、处罚等）均由使用者自行承担，项目开发者不承担任何责任。
3. 本项目仅调用公开的接口/模拟人工操作，不涉及破解、入侵抖音系统，使用者需遵守《抖音用户服务协议》及相关法律法规。
4. 请合理控制脚本运行频率，避免给抖音平台服务器造成压力，建议仅用于个人少量好友的火花维系。
5. 若你使用本项目即表示已阅读并同意本免责声明，如不同意请立即停止使用。

## 📄 开源协议

本项目基于 MIT 协议开源，你可以自由使用、修改和分发本项目代码，详见 [LICENSE](LICENSE) 文件。
