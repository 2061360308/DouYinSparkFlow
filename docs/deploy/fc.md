# 云函数部署

用阿里云函数计算（FC 3.0）部署，无需服务器，按量付费。

> ⚠️ **该方式目前尚未经过测试，可用性未知。** 适合有动手能力、愿意折腾的用户先行验证，欢迎反馈结果。

## 原理

用两个函数，跑在同一地域（默认 `cn-hangzhou`），保证出口 IP 一致：

| 函数 | 镜像 | 作用 |
| --- | --- | --- |
| `DYSparkTask` | `douyinsparkflow` | 定时触发器驱动，跑续火任务 |
| `DYSparkGost` | `gost` | HTTP 触发器暴露 WebSocket 隧道，给本地 configTool 借出口 |

## 为什么要有 gost 函数

FC 的 HTTP 触发器不支持 CONNECT 方法，浏览器没法直接把它当 HTTP 代理。所以链路是：

```
浏览器 → 本地 gost（HTTP 代理，处理 CONNECT）→ wss 隧道 → FC 网关 → gost 函数(:9000) → 目标站
```

这样本地抓 Cookie 的出口就落在云函数所在地域，与跑任务的出口一致。

## 部署步骤

1. 在阿里云 ROS 控制台导入 [`aliyun-fc-ros-template.yaml`](https://github.com/2061360308/DouYinSparkFlow/blob/main/aliyun-fc-ros-template.yaml)
2. 填写参数（函数名、镜像地址、规格、cron 表达式等），保持默认地域 `cn-hangzhou`
3. 关键参数：
   - `EnvPayload`：触发消息 payload，填 `.env` 全文（FC 环境变量上限 4KB，装不下 COOKIES，配置只能走触发消息）
   - `GostPassword`：务必改掉默认值
4. 部署完成后，从输出里拿到 gost HTTP 触发器的公网地址

## 给 configTool 配隧道

拿到 gost 触发器的公网地址（形如 `https://xxx.cn-hangzhou.fc.aliyuncs.com`）后，在 configTool「工具配置」页签填：

| 项 | 值 |
| --- | --- |
| 隧道地址 | `wss://<账号>:<密码>@<去掉 https:// 的域名>:443?path=/ws` |

## 费用与规格

- vCPU : 内存需在 1:1 ~ 1:4 之间，0.5 核最少 512MB
- gost 是纯 TCP 转发，0.35 核 / 512MB 足够；单实例并发度取 50（每条隧道占 1 个并发）
- 计费按实例实际活跃时长，抓完记得关掉本地 gost 客户端
- 不配日志则不产生 SLS 费用；但排错时需要临时到「高级配置 → 日志」开启

> 不建议给函数配健康检查：探针会每隔几秒打请求，导致实例跑完也不回收，按 24 小时计费。
