# 源码部署

适合高级用户，或想在本机、云服务器、青龙 / 白虎等任务管理面板环境跑的情况。

> 无头浏览器模拟人工操作，对设备性能和浏览器运行环境有一定要求，实际表现以测试为准。

## 1. 克隆项目

```bash
git clone https://github.com/2061360308/DouYinSparkFlow.git
cd DouYinSparkFlow
```

## 2. 安装依赖

```bash
pip install -r requirements.txt
```

## 3. 准备配置

在项目根目录创建 `.env`，内容用 configTool 生成（见 [配置生成器](guide/03-配置生成器.md)），或用网页版生成后粘贴。

## 4. 运行

```bash
python main.py task
```

不带参数执行 `python main.py` 等价于 `task` 模式。

## 5. 定时运行

源码部署不自带调度，需要你自己配：

- Linux 服务器：用 cron
- 任务面板（青龙 / 白虎）：按面板的方式添加定时任务，执行 `python main.py task`

## 关于出口 IP

本机运行的话，「抓 Cookie 的出口」和「跑任务的出口」都是本机，天然一致。但如果 Cookie 是在别处抓的、或用网页版生成的，注意出口 IP 是否一致（见 [Cookie 与出口 IP](guide/02-cookie与出口IP.md)）。

## 本地调试

如果你要改代码、跑测试、或通过代理调试，见 [开发](dev/overview.md) 篇。
