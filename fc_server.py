r"""云函数（FC）模式下的 HTTP Server —— 只干一件事：接住定时触发器事件，跑一轮 runTasks。

为什么云函数也必须有 HTTP Server：
    自定义镜像函数的运行形态是「容器里常驻一个 HTTP 服务」，平台的**所有**请求都是
    通过 HTTP 打到容器监听的端口上，靠请求头 ``x-fc-control-path`` 区分来源：

        /invoke        事件函数调用 —— **定时触发器走的就是这条**
        /http-invoke   HTTP 触发器调用 —— 本项目不配 HTTP 触发器，不会来
        /initialize    Initializer 回调 —— 只在函数配置了 Initializer 时才会发

    同时在容器里放 cron 是没意义的：函数实例按请求存在，没请求就没有实例。

配置从哪来（重要）：
    FC 函数环境变量总上限 4 KB，塞不下 COOKIES_* 这类 JSON 大值，所以本项目支持把
    整个 .env 文本放进定时触发器的「触发消息」里，Server 收到后解析成环境变量再跑
    任务。触发消息形如：

        {"triggerTime": "...", "triggerName": "...", "payload": "<.env 全文>"}

    ⚠️ 触发消息本身是 JSON，所以往 payload 里写 .env 时要注意转义：
        - 换行必须写成两个字符「\ n」，到达时会被 JSON 还原成真换行；若整段一个真
          换行都没有（说明换行没转义），Server 会按字面「\ n」兜底还原一次。
        - **所有反斜杠都要双写** —— COOKIES_* 里的 \uXXXX、MESSAGE_TEMPLATE 里的
          \ n 分别是「\\uXXXX」「\\n」，少写一个反斜杠就会被 JSON 先吃掉，
          变成真字符/真换行，配置就废了。
    解析出来的键直接覆盖进程环境变量，因此 payload 的优先级高于镜像里 /app/.env。
    若触发消息里没有 .env 内容，则沿用进程已有的环境变量（含 /app/.env），行为不变。

平台硬性要求（不满足直接 FunctionNotStarted / 请求超时）：
    1. 必须监听 0.0.0.0:CAPort（默认 9000），监听 127.0.0.1 会被判定启动失败；
    2. Server 必须在 120 秒内启动完毕（纯 stdlib，秒起）；
    3. 连接要 Keep-Alive，Server 端超时 ≥ 15 分钟（见 Handler.timeout）。

本地自测（不需要任何 FC 环境）：
    python main.py fc
    curl -X POST localhost:9000/invoke -d '{}'
    curl -X POST localhost:9000/invoke -d '{"payload":"TASKS=[]\nFOO=bar"}'
"""

import io
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# CAPort：函数配置里填 9000；平台运行时会注入 FC_SERVER_PORT / FC_CUSTOM_LISTEN_PORT
PORT = int(os.getenv("FC_SERVER_PORT") or os.getenv("FC_CUSTOM_LISTEN_PORT") or 9000)

# 同一实例内只允许跑一轮：单实例并发度建议配 1，这里只是兜底
_run_lock = threading.Lock()

# 定时触发器事件体里会被透传的字段（FC 官方格式）
EVENT_FIELDS = ("triggerTime", "triggerName", "payload")

# 触发消息里可能承载 .env 文本的字段名，按优先级排列
ENV_TEXT_KEYS = (".env", "env", "dotenv", "envText", "env_text", "text", "content", "payload")


def log(message):
    """打印到 stdout 的内容会被 FC 自动收集进日志服务（SLS）。"""
    print(f"[fc] {message}", flush=True)


# ---------------------------------------------------------------------------
# 触发消息 → 环境变量
# ---------------------------------------------------------------------------

def _try_json(text):
    """文本长得像 JSON 就解析，否则返回 None（不抛异常）。"""
    stripped = text.lstrip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _extract_env_source(event):
    """把触发消息归一成 (.env 文本, 键值对字典)，两者至少一个为空。

    控制台上手写 JSON 很容易把结构写歪，所以能认的形态都认下来：
        payload: "<.env 全文>"                        → 文本
        payload: "{\\"env\\": \\"<.env 全文>\\"}"     → 文本（payload 里又套了一层 JSON 文本）
        payload: {"env"/".env"/"text"/...: "..."}     → 文本
        payload: {"TASKS": "...", ...}                → 键值对
        event 自己就是字符串                           → 当成 payload
    """
    payload = event.get("payload") if isinstance(event, dict) else event

    if isinstance(payload, str):
        inner = _try_json(payload)
        if isinstance(inner, (dict, list)):
            payload = inner  # payload 还是 JSON 文本，继续往下拆
        else:
            return payload, {}

    if isinstance(payload, dict):
        for key in ENV_TEXT_KEYS:
            value = payload.get(key)
            if isinstance(value, str):
                return value, {}
        # 没有文本字段，那就当它本身是「键 → 值」的配置映射
        return "", {str(k): v for k, v in payload.items() if isinstance(v, str)}

    return "", {}


def _parse_env_text(text):
    """用 python-dotenv 自带的解析器把文本读成 dict（只解析，不落盘、不碰 os.environ）。

    复用 dotenv 而不是自己 split("=")：引号、`export ` 前缀、`#` 注释这些边角都由它
    兜住，和 main.py 读 /app/.env 走的是同一套语义。
    """
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(stream=io.StringIO(text)).items() if v is not None}


def _inject_env(text):
    """解析 .env 文本并写入 os.environ，返回注入的键名列表（排序后）。"""
    if not text or not text.strip():
        return []

    parsed = _parse_env_text(text)

    # 整段没有一个真换行、却含字面「\ n」，说明触发消息里的换行没被 JSON 转义，
    # 整份配置挤成了一行，按字面还原一次再解析。
    #
    # 反过来，只要文本里已经有真换行，就**绝不能**再解转义：MESSAGE_TEMPLATE 必须
    # 保留字面的「\ n」（core/tasks.py 靠它 split 逐行输入），解转义会把整份 .env
    # 的行结构拆烂。所以这里只做「一行式」的兜底，用解析出的键数多者胜出来防误判。
    if "\n" not in text and "\\n" in text:
        restored = text.replace("\\r\\n", "\n").replace("\\n", "\n")
        restored_parsed = _parse_env_text(restored)
        if len(restored_parsed) > len(parsed):
            log("触发消息里的 .env 换行未转义，已按字面「\\n」还原")
            parsed = restored_parsed

    if not parsed:
        log("警告: 触发消息里的 payload 没能解析出任何 KEY=VALUE，配置未注入")
        return []

    os.environ.update(parsed)  # 直接覆盖，payload 优先于镜像里的 /app/.env
    return sorted(parsed)


def _apply_event_env(event):
    """把触发消息里的配置灌进 os.environ，返回注入的键名列表。"""
    text, pairs = _extract_env_source(event or {})

    if text:
        keys = _inject_env(text)
    elif pairs:
        os.environ.update(pairs)
        keys = sorted(pairs)
    else:
        log("触发消息里没有 .env 内容，沿用进程环境变量（含 /app/.env）")
        return []

    if not keys:
        return []

    # 只打键名，绝不打值：COOKIES_* 的值就是 sessionid
    log(f"已从触发消息注入 {len(keys)} 个环境变量: {', '.join(keys)}")

    if "core.tasks" in sys.modules:
        # core.tasks 在模块顶层就把 config / userData 读死了，热实例里再改环境变量
        # 它不会重读。FC 会复用实例，所以第二次触发拿到的可能仍是上一次的配置。
        log(
            "警告: core.tasks 已在本实例内加载过，本次注入的新配置不会生效；"
            "需要立即生效请发新版本或等实例回收"
        )

    return keys


def _size_of(value):
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def _summarize(event):
    """给日志用的摘要 —— payload 装的是 .env 全文（含 sessionid），绝不能整段打出去。"""
    if not isinstance(event, dict):
        return f"<非对象事件 {type(event).__name__}>"
    if not event:
        return "<空事件>"
    parts = []
    for key, value in event.items():
        if key == "payload":
            parts.append(f"payload=<{_size_of(value)} 字符>")
        else:
            parts.append(f"{key}={value!r}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

def run_once(event=None):
    """跑一轮任务，返回 (HTTP 状态码, 响应体字典)。

    event 是平台的触发消息；若其中带 .env 文本，会在 import core.tasks **之前**
    注入 os.environ —— core.tasks 模块顶层就读环境变量，晚一步就白搭。

    延迟 import core.tasks 的另一个原因：提前 import 会让「把 Server 起起来」强依赖
    配置，配置缺失时连健康检查都过不去。
    """
    if not _run_lock.acquire(blocking=False):
        log("已有任务在跑，跳过本次触发")
        return 409, {"ok": False, "error": "already running"}

    try:
        env_keys = _apply_event_env(event)

        from core.tasks import runTasks

        log("开始执行 runTasks")
        runTasks()
        log("runTasks 执行结束")
        return 200, {"ok": True, "env_keys": env_keys}
    except Exception as exc:  # noqa: BLE001 —— 必须吞掉并回 5xx，否则平台只看到连接断开
        traceback.print_exc()
        log(f"runTasks 执行失败: {exc!r}")
        return 500, {"ok": False, "error": repr(exc)}
    finally:
        _run_lock.release()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

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
        """保留平台透传字段（含 payload 原件，run_once 要拿它解析配置）。"""
        if not raw:
            return {}
        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # 不是 JSON：当成裸 payload 交给下游，至少别把它丢了
            return {"payload": raw.decode("utf-8", "replace")}
        if not isinstance(event, dict):
            return {"payload": event}
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
            event = self._parse_event(raw)
            # 只打摘要：payload 里是 .env 全文，直接打会把 cookie 写进日志
            log(f"收到定时触发器事件: {_summarize(event)}")
            status, payload = run_once(event)
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
    log("提示：配置可放在定时触发器「触发消息」的 payload 里（.env 全文）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("收到中断信号，退出")
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
