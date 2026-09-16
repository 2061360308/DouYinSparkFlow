"""用 gost 把本地浏览器接到云函数侧的 WebSocket 隧道。

为什么需要它：
    FC 的 HTTP 触发器不支持 CONNECT，浏览器没法直接把函数域名当代理用。所以本地得跑一个
    gost，把浏览器的 HTTP 代理请求（含 CONNECT）封进 wss 隧道，送到云函数里的 gost，
    由它去连目标站 —— 这样浏览器的出口 IP 就落在云函数所在地域。

生命周期与浏览器会话绑定（用完即释放，云函数那边连接断开就不再计费）：
    tunnel = GostTunnel(tunnel_url, user, password, log=print)
    local_proxy = tunnel.start()        # "http://127.0.0.1:54321"
    ...（用 local_proxy 启动浏览器）...
    tunnel.stop()

实现要点：
  - 本地监听端口每次随机挑一个空闲端口，避免固定端口被别的程序占用；
  - 「就绪」判定为本地端口能连上；进程早退会立刻报错并把 gost 的最后一句话带出来；
  - gost 的输出被后台线程读进 log 回调，排错时能看到「隧道连不上」这类原因；
  - 进程登记在模块级表里，程序异常退出时由 atexit 兜底清理，不留野进程。
"""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import paths

# 等待隧道就绪的最长时长（秒）：冷启动 + TLS 握手 + ws 升级都在这里
READY_TIMEOUT = 20.0
READY_POLL = 0.25
# 关停时留给 gost 退出的时间（秒）
STOP_TIMEOUT = 5.0

# 活着的隧道实例；进程退出时兜底 stop
_LIVE_TUNNELS: set = set()
_ATEXIT_HOOKED = False


def _hook_atexit() -> None:
    global _ATEXIT_HOOKED
    if _ATEXIT_HOOKED:
        return
    _ATEXIT_HOOKED = True

    def _cleanup() -> None:
        for tunnel in list(_LIVE_TUNNELS):
            try:
                tunnel.stop()
            except Exception:
                pass

    atexit.register(_cleanup)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def free_port() -> int:
    """挑一个当前空闲的本地端口（绑 0 让系统分配，随即释放）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_forward_url(tunnel: str, user: str = "", password: str = "") -> str:
    """把「隧道地址 + 账号密码」拼成 gost 的 -F 地址。

      - 地址里已经带了凭据（含 @）就原样用，不重复插；
      - 账号密码做 percent-encode，密码里出现 @ : / ? 也不会破坏 URL；
      - 没写 path 时补 ?path=/ws（必须与云函数里 gost 的监听参数一致）；
      - 顺手补上 keepAlive/ttl：平台会按空闲超时掐断长连接，心跳能明显减少断连。
    """
    tunnel = (tunnel or "").strip()
    if not tunnel:
        raise ValueError("隧道地址为空")
    if not tunnel.startswith(("ws://", "wss://")):
        raise ValueError("隧道地址要以 ws:// 或 wss:// 开头")

    scheme, _, rest = tunnel.partition("://")
    user = (user or "").strip()
    password = (password or "").strip()
    if "@" not in rest and user:
        cred = urllib.parse.quote(user, safe="")
        if password:
            cred += ":" + urllib.parse.quote(password, safe="")
        rest = f"{cred}@{rest}"

    url = f"{scheme}://{rest}"
    if "path=" not in url:
        url += ("&" if "?" in url else "?") + "path=/ws"
    if "keepAlive" not in url:
        url += "&keepAlive=true&ttl=15s"
    return url


def mask_credentials(url: str) -> str:
    """把 URL 里的密码抹成 ***，用于打日志。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return url
    if not parts.password:
        return url
    user = urllib.parse.quote(parts.username or "")
    host = parts.hostname or ""
    netloc = f"{user}:***@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


# ---------------------------------------------------------------------------
# 隧道
# ---------------------------------------------------------------------------
class GostTunnel:
    """一个 gost 客户端进程 = 一条通往云函数的隧道。"""

    def __init__(
        self,
        tunnel: str,
        user: str = "",
        password: str = "",
        *,
        gost_path: str = "",
        log=None,
    ) -> None:
        self.forward_url = build_forward_url(tunnel, user, password)
        self.gost_path = paths.gost_binary(gost_path) if gost_path else paths.gost_binary()
        self.log = log or (lambda text: None)
        self.port = 0
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._last_line = ""

    # -- 状态 ---------------------------------------------------------------
    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.port}" if self.port else ""

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # -- 启停 ---------------------------------------------------------------
    def start(self) -> str:
        """启动 gost 并等本地端口就绪，返回可交给浏览器 proxy 参数的地址。"""
        if not Path(self.gost_path).is_file():
            raise RuntimeError(
                f"找不到 gost 可执行文件：{self.gost_path}\n"
                f"请到 {paths.GOST_RELEASES_URL} 下载 windows_amd64 包，"
                "解压出 gost.exe 放到程序目录（或在界面上填「gost 程序路径」）。"
            )

        self.port = free_port()
        args = [
            str(self.gost_path),
            "-L",
            f"http://127.0.0.1:{self.port}",
            "-F",
            self.forward_url,
        ]

        # 打包成 exe 后最忌弹出黑色控制台窗口，Windows 下显式隐藏
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self.log(f"启动隧道：gost -L http://127.0.0.1:{self.port} -F {mask_credentials(self.forward_url)}")
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )

        _hook_atexit()
        _LIVE_TUNNELS.add(self)
        self._reader = threading.Thread(
            target=self._pump_output, daemon=True, name="gost-output"
        )
        self._reader.start()

        try:
            self._wait_ready()
        except Exception:
            self.stop()
            raise

        self.log(f"隧道已就绪，本地代理：{self.proxy_url}")
        return self.proxy_url

    def stop(self) -> None:
        """关停 gost。幂等，重复调用无副作用。"""
        proc = self.proc
        self.proc = None
        _LIVE_TUNNELS.discard(self)

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=STOP_TIMEOUT)
            except Exception:
                pass
            self.log("隧道已释放")

        self.port = 0

    # -- 内部 ---------------------------------------------------------------
    def _pump_output(self) -> None:
        """把 gost 的输出转进日志回调（它把一切写在 stderr，这里已并到 stdout）。"""
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                text = str(line or "").strip()
                if text:
                    self._last_line = text
                    self.log(f"[gost] {text}")
        except Exception:
            pass

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(
                    f"gost 启动后立即退出（退出码 {self.proc.returncode}）{self._tail()}"
                )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                if sock.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(READY_POLL)
        raise RuntimeError(f"等待隧道就绪超时（{READY_TIMEOUT:.0f} 秒）{self._tail()}")

    def _tail(self) -> str:
        return f"\ngost 最后输出：{self._last_line}" if self._last_line else ""
