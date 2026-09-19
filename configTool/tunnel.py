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
import urllib.request
from pathlib import Path

from configTool import paths

# 等待隧道就绪的最长时长（秒）：冷启动 + TLS 握手 + ws 升级都在这里
READY_TIMEOUT = 20.0
READY_POLL = 0.25
# 关停时留给 gost 退出的时间（秒）
STOP_TIMEOUT = 5.0
# 启动尝试次数。gost 对单个节点有「失败即摘除」的行为：一旦某次拨号失败，节点会被
# 打上不可用标记（failTimeout 内不再尝试），同一个进程里怎么重试都只会一路报
# none node available —— 必须换一个新进程才谈得上重试。
START_ATTEMPTS = 3
# 预热目标：轻量、国内可达，用来确认隧道真的能出网
WARMUP_TARGET = "https://www.baidu.com"
# 预热请求超时（秒）：这一步会实打实走一趟隧道，函数冷启动的等待都算在这里
WARMUP_TIMEOUT = 40.0

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
      - **没写端口时按协议补默认端口（wss→443 / ws→80）**：gost 不会替 ws/wss 兜
        默认端口，少个端口就是拨号失败，而失败会被"节点摘除"放大成一片
        none node available —— 界面上只剩下 ERR_TUNNEL_CONNECTION_FAILED；
      - 没写 path 时补 ?path=/ws（必须与云函数里 gost 的监听参数一致）；
      - 顺手补上 keepAlive/ttl：平台会按空闲超时掐断长连接，心跳能明显减少断连；
      - 再补一个放宽的 handshakeTimeout：函数冷启动时首次握手可能要等几秒。
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

    # 补默认端口：gost 自己不会兜 ws/wss 的端口，缺了就是"拨号失败"（见 docstring）
    parts = urllib.parse.urlsplit(url)
    if parts.port is None:
        cred = ""
        if parts.username:
            cred = urllib.parse.quote(parts.username, safe="")
            if parts.password:
                cred += ":" + urllib.parse.quote(parts.password, safe="")
            cred += "@"
        netloc = "%s%s:%s" % (cred, parts.hostname or "", "80" if parts.scheme == "ws" else "443")
        url = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))

    if "path=" not in url:
        url += ("&" if "?" in url else "?") + "path=/ws"
    if "keepAlive" not in url:
        url += "&keepAlive=true&ttl=15s"
    if "handshakeTimeout" not in url:
        url += "&handshakeTimeout=60s"
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
        """启动 gost、确认真的能出网，然后返回可交给浏览器 proxy 参数的地址。

        为什么不是"起来就用"：函数冷启动时第一次握手可能很慢（实测同一地址首次
        35 秒超时、紧接着第二次只要 4 秒），而 gost 对单个节点是"失败即摘除" ——
        一旦某次拨号被判失败，同一个进程里之后所有请求都只会报 none node available，
        浏览器拿到的是一条已经残废的隧道（ERR_TUNNEL_CONNECTION_FAILED）。

        所以这里自己先打一枪（预热）：通了才交给浏览器；不通就换一个新进程重来，
        最多 START_ATTEMPTS 次。这样"慢一点"总比"打开就失败"强。
        """
        if not Path(self.gost_path).is_file():
            raise RuntimeError(
                f"找不到 gost 可执行文件：{self.gost_path}\n"
                f"请到 {paths.GOST_RELEASES_URL} 下载 windows_amd64 包，"
                "解压出 gost.exe 放到程序目录（或在界面上填「gost 程序路径」）。"
            )

        last_error = ""
        for attempt in range(1, START_ATTEMPTS + 1):
            self._spawn()
            try:
                self._wait_ready()
                self._warmup()
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < START_ATTEMPTS:
                    self.log(f"隧道第 {attempt}/{START_ATTEMPTS} 次未通，换个新进程重试…")
                self.stop()
                time.sleep(1.0)
                continue
            self.log(f"隧道已就绪，本地代理：{self.proxy_url}")
            return self.proxy_url

        raise RuntimeError(
            f"配套代理连不通（已试 {START_ATTEMPTS} 次）：{last_error}\n"
            "常见原因：云函数冷启动过慢、隧道地址/账号密码不对、或云函数侧没起来。"
        )

    def _spawn(self) -> None:
        """拉起一个 gost 进程并接上日志泵（只负责起进程，不做可用性判断）。"""
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

        self.log(
            f"启动隧道：gost -L http://127.0.0.1:{self.port} -F {mask_credentials(self.forward_url)}"
        )
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

    def _warmup(self, target: str = WARMUP_TARGET, timeout: float = WARMUP_TIMEOUT) -> None:
        """通过刚建好的本地代理真发一次请求，确认这条隧道端到端能通。

        除了挡冷启动，它还能顺手把"凭据错了"这类问题在开浏览器之前就暴露出来
        （那种情况会一路失败到重试上限，报错里带上具体原因）。
        """
        handler = urllib.request.ProxyHandler(
            {"http": self.proxy_url, "https": self.proxy_url}
        )
        opener = urllib.request.build_opener(handler)
        try:
            with opener.open(target, timeout=timeout) as resp:
                resp.read(64)
        except Exception as exc:
            raise RuntimeError(
                f"出口连通性检查未通过（{target}）：{type(exc).__name__}: {exc}"
            ) from exc

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
