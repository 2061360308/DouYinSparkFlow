# DouYin Spark Flow

![cover](images/cover-transparent.png ":size=640x320")

抖音火花自动续火脚本，通过无头浏览器模拟人工操作，定时给好友发送消息来续火花。

## 特性

- 多账号、多目标，一个账号可同时给多个好友续火花
- 支持按备注 / 昵称 / 抖音号匹配目标好友
- 支持「每日一言」消息模板
- 本地配置生成器（configTool）自动抓取登录态，无需手动导出 Cookie

## 快速开始

1. 先看 [选择部署方式](guide/01-选择部署方式.md)，按自己的情况挑一种
2. 用 [配置生成器](guide/03-配置生成器.md) 生成 `.env`
3. 按对应的部署文档操作

三种部署方式：**服务器 Docker（最推荐）**、云函数（少量付费、可用性待验证）、GitHub Action（已过时）。

## 链接

- [GitHub 仓库](https://github.com/2061360308/DouYinSparkFlow)
- [讨论区](https://github.com/2061360308/DouYinSparkFlow/discussions)
