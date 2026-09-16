"""云函数（FC）模式下的 HTTP Server —— 只干一件事：接住定时触发器事件，跑一轮 runTasks。

为什么云函数也必须有 HTTP Server：
    自定义镜像函数的运行形态是「容器里常驻一个 HTTP 服务」，平台的**所有**请求都是
    通过 HTTP 打到容器监听的端口上，靠请求头 ``x-fc-control-path`` 区分来源：

        /invoke        事件函数调用 —— **定时触发器走的就是这条**
        /http-invoke   HTTP 触发器调用 —— 本项目不配 HTTP 触发器，不会来
        /initialize    Initializer 回调 —— 只在函数配置了 Initializer 时才会发

    同时在容器里放 cron 是没意义的：函数实例按请求存在，没请求就没有实例。

平台硬性要求（不满足直接 FunctionNotStarted / 请求超时）：
    1. 必须监听 0.0.0.0:CAPort（默认 9000），监听 127.0.0.1 会被判定启动失败；
    2. Server 必须在 120 秒内启动完毕（纯 stdlib，秒起）；
    3. 连接要 Keep-Alive，Server 端超时 ≥ 15 分钟（见 Handler.timeout）。

本地自测（不需要任何 FC 环境）：
    python main.py fc
    curl -X POST localhost:9000/invoke -d '{}'
"""

import json
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# CAPort：函数配置里填 9000；平台运行时会注入 FC_SERVER_PORT / FC_CUSTOM_LISTEN_PORT
PORT = int(os.getenv("FC_SERVER_PORT") or os.getenv("FC_CUSTOM_LISTEN_PORT") or 9000)

# 同一实例内只允许跑一轮：单实例并发度建议配 1，这里只是兜底
_run_lock = threading.Lock()

# 定时触发器事件体里会被透传的字段（FC 官方格式）
EVENT_FIELDS = ("triggerTime", "triggerName", "payload")


def log(message):
    """打印到 stdout 的内容会被 FC 自动收集进日志服务（SLS）。"""
    print(f"[fc] {message}", flush=True)


def run_once():
    """跑一轮任务，返回 (HTTP 状态码, 响应体字典)。

    延迟 import core.tasks：它模块顶层就调 get_config()/get_userData() 读环境变量，
    提前 import 会让「把 Server 起起来」强依赖配置，配置缺失时连健康检查都过不去。
    """
    if not _run_lock.acquire(blocking=False):
        log("已有任务在跑，跳过本次触发")
        return 409, {"ok": False, "error": "already running"}

    try:
        from core.tasks import runTasks

        log("开始执行 runTasks")
        runTasks()
        log("runTasks 执行结束")
        return 200, {"ok": True}
    except Exception as exc:  # noqa: BLE001 —— 必须吞掉并回 5xx，否则平台只看到连接断开
        traceback.print_exc()
        log(f"runTasks 执行失败: {exc!r}")
        return 500, {"ok": False, "error": repr(exc)}
    finally:
        _run_lock.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # 平台要求 Keep-Alive；HTTP/1.0 默认每次都关连接
    timeout = 1800  # 平台要求 Server 端超时 ≥ 15 分钟

    # ---- 响应工具 ---------------------------------------------------------
    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # HTTP/1.1 下必须显式给长度，否则客户端会一直等
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _control_path(self):
        """优先看平台注入的 x-fc-control-path；本地直连时退回 URL path。"""
        return self.headers.get("x-fc-control-path") or self.path

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    @staticmethod
    def _parse_event(raw):
        if not raw:
            return {}
        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"raw": raw.decode("utf-8", "replace")}
        if not isinstance(event, dict):
            return {"raw": event}
        return {k: event[k] for k in EVENT_FIELDS if k in event}

    # ---- 路由 -------------------------------------------------------------
    def do_GET(self):
        # 健康检查、平台探测、本地开浏览器看一眼，统统 200
        self._reply(200, {"status": "ok", "mode": "fc", "port": PORT})

    def do_POST(self):
        try:
            raw = self._read_body()
        except Exception as exc:  # noqa: BLE001
            self._reply(400, {"ok": False, "error": f"读取请求体失败: {exc!r}"})
            return

        path = self._control_path()
        if path.startswith("/initialize"):
            # 只在函数配置了 Initializer 回调时平台才会发，不配就永远走不到这里
            log("收到 /initialize")
            self._reply(200, {"ok": True})
        elif path.startswith("/invoke"):
            log(f"收到定时触发器事件: {self._parse_event(raw)}")
            status, payload = run_once()
            self._reply(status, payload)
        else:
            # /http-invoke 之类：本项目不配 HTTP 触发器，明确拒绝，别误以为跑过了
            self._reply(404, {"ok": False, "error": f"不支持的调用路径: {path}"})

    def log_message(self, fmt, *args):
        # 默认往 stderr 写且不带前缀；统一搬到 stdout，方便和业务日志连起来看
        log(f"{self.address_string()} {fmt % args}")


def serve():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"HTTP Server 已启动，监听 0.0.0.0:{PORT}，等待定时触发器事件")
    log("提示：函数配置里的「监听端口」必须与这个端口一致")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("收到中断信号，退出")
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
