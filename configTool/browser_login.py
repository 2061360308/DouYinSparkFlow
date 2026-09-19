"""cloakbrowser 登录会话工作线程（无 GUI 依赖）。

设计要点：
  - cloakbrowser 底层是 Playwright 同步 API，有线程亲和性，必须在同一个线程里
    完成全部调用。因此本模块把浏览器操作封在一个后台线程里，外部只通过
    「命令队列 + 事件队列」与它交互，绝不跨线程直接碰 page / context。
  - 每个账户传入自己的 profile 目录，登录态相互隔离，可分别重新登录。
  - 登录态判定与会话枚举都借主程序的 core/douyin_im.py，不在这里镜像其选择器。

用法：
    worker = BrowserLoginWorker(profile_dir, fingerprint="73841")
    worker.start()
    worker.send("open")           # 启动浏览器并跳转抖音聊天页
    worker.send("probe")          # 廉价探一次：登录了吗 / 账号信息截到了吗（供自动流程轮询）
    worker.send("grab")           # 抓取登录态（自动流程）
    worker.send("grab", {"allow_reload": True, "deep_login": True})  # 手动「立即抓取」
    worker.send("conversations")  # 枚举全部会话（交给 core.douyin_im 完成）
    worker.send("shutdown")       # 关闭浏览器并结束线程

    然后从 worker.events 里取
    ("log"|"status"|"opened"|"probe"|"grabbed"|"conversations"|
     "conversation_progress"|"error"|"done", payload)

    probe / grabbed 的 payload 给两个口径：logged_in 是本地有没有 sessionid
    （抓取门禁只看它），login_state 是 core.douyin_im 的判定（只管提示与拦截）。

指纹与配置目录都取自 profiles.json（见 profile_store.py）。fingerprint 传空则退回
cloakbrowser 的默认行为 —— 每次启动随机一个新指纹。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path

from configTool import local_settings, paths
from configTool.tunnel import GostTunnel

# 主程序的会话扫描 / 登录态判定实现 —— 「抖音页面怎么点、怎么滚」全项目只有这一份。
# 顶层导入依赖「仓库根在 sys.path[0] 上」，由仓库根的入口 run_configtool.py 保证；
# 打包时 PyInstaller 会顺着这个 import 把它一起收进包（见 build-configtool.yml）。
import core.douyin_im as douyin_im

# 与 core.douyin_im.CHAT_URL 同一份，别再各写一个字符串
CHAT_URL = douyin_im.CHAT_URL

# 浏览器指纹种子开关。cloakbrowser 默认每次启动都随机一个种子
# （config.py::get_default_stealth_args 里的 random.randint(10000, 99999)），
# 同一个账号天天换指纹，在风控眼里就是「同一个人不停换设备」。
# 我们在 args 里显式传同一个值把它钉死 —— cloakbrowser 的 _combine_args 按
# '=' 前的键去重，调用方传的同名 flag 覆盖内部默认值，其余隐身参数照常生效。
FINGERPRINT_FLAG = "--fingerprint="

# 账号信息接口的两个等待时长（秒）
#   GRACE：先原地等一会儿。goto 返回时请求往往还在飞行中，等几百毫秒就有了，
#          没必要立刻刷新页面（headful 下刷新是可见的，很打扰人）。
#   WAIT ：刷新之后愿意等多久。
PROFILE_GRACE_SECONDS = 5.0
PROFILE_WAIT_SECONDS = 15.0

# 打开抖音聊天页时愿意等首屏多久（毫秒）。走配套代理时链路抖动很大，给宽些。
GOTO_TIMEOUT_MS = 180_000

# 只保留这些域下的 Cookie，避免把无关站点的 Cookie 灌进任务
TARGET_DOMAINS = ("douyin.com", "bytedance.com", "snssdk.com", "iesdouyin.com", "amemv.com")

# 出现任意一个即可判定为已登录
LOGIN_COOKIE_NAMES = ("sessionid", "sessionid_ss", "sid_tt")

# 账号信息接口。抖音自己会在页面加载时发这个请求，我们从响应里取昵称与抖音号。
# 注意：不要在页面里主动 fetch 它 —— 实测缺 a_bogus 签名时服务端直接回
# {"status_code": 8, "status_msg": "用户未登录"}，只有抖音自己的请求才是有效的。
SELF_PROFILE_API = "/aweme/v1/web/user/profile/self"

# 自带的隐身 Chromium（与本模块同级目录），跳过联网下载
BROWSER_BINARY = paths.browser_binary()
BROWSER_MISSING = not BROWSER_BINARY.is_file()

if not BROWSER_MISSING:
    os.environ.setdefault("CLOAKBROWSER_BINARY_PATH", str(BROWSER_BINARY))
    os.environ.setdefault("CLOAKBROWSER_AUTO_UPDATE", "false")
# 浏览器缺失时不写死路径，交给 cloakbrowser 自己的解析逻辑，报错信息更准确

_launch_persistent_context = None
IMPORT_ERROR = ""


def _ensure_dependencies_on_path() -> None:
    """开发期便利：允许用任意解释器启动。

    只要程序旁边（或上一层）存在 .venv，就把它的 site-packages 借过来，
    这样 `python main.py` 用系统解释器也能找到 cloakbrowser。
    打包成 exe 后这些目录不存在，本函数自然跳过，不影响发布版。
    """
    import sys

    for root in (paths.APP_DIR, paths.APP_DIR.parent):
        for rel in ("Lib/site-packages", "lib/site-packages"):
            candidate = root / ".venv" / rel
            if candidate.is_dir() and str(candidate) not in sys.path:
                sys.path.append(str(candidate))
        lib = root / ".venv" / "lib"
        if lib.is_dir():
            for candidate in lib.glob("python*/site-packages"):
                if candidate.is_dir() and str(candidate) not in sys.path:
                    sys.path.append(str(candidate))


def load_cloakbrowser():
    """延迟导入 cloakbrowser；失败时把原因记在 IMPORT_ERROR 里返回 None。"""
    global _launch_persistent_context, IMPORT_ERROR

    if _launch_persistent_context is not None or IMPORT_ERROR:
        return _launch_persistent_context

    _ensure_dependencies_on_path()
    try:
        from cloakbrowser import launch_persistent_context
    except Exception as exc:
        IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        return None

    _launch_persistent_context = launch_persistent_context
    return _launch_persistent_context


def environment_hint() -> str:
    """依赖/资源缺失时给用户的排查提示。"""
    lines = []
    if IMPORT_ERROR:
        lines.append(f"cloakbrowser 导入失败：{IMPORT_ERROR}")
    if BROWSER_MISSING:
        lines.append(f"未找到自带浏览器：{BROWSER_BINARY}")
    if lines:
        lines.append("请确认本目录完整（含 cloakbrowser-windows-x64），且已安装依赖。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cookie 处理
# ---------------------------------------------------------------------------
def matches_target(domain: str) -> bool:
    domain = (domain or "").lower().lstrip(".")
    return any(domain == d or domain.endswith("." + d) for d in TARGET_DOMAINS)


def clean_cookie(cookie: dict):
    """把一条 cookie 裁剪成 Playwright add_cookies 需要的字段。

    返回 None 表示这条**不可用**，调用方应丢弃：
      - name 为空：站点确实会产生这种条目（实测抖音上就有一条 value="douyin.com"
        的空名 cookie），而 add_cookies 遇到空 name 会让整批注入失败；
      - domain 为空：没有归属域，Playwright 同样不接受。

    去掉 sameSite 与主项目 utils/config.py::sanitize_cookies 的口径保持一致。
    """
    name = str(cookie.get("name") or "").strip()
    domain = str(cookie.get("domain") or "").strip()
    if not name or not domain:
        return None
    return {
        "name": name,
        "value": cookie.get("value", ""),
        "domain": domain,
        "path": cookie.get("path") or "/",
        "expires": cookie.get("expires", -1),
        "httpOnly": bool(cookie.get("httpOnly", False)),
        "secure": bool(cookie.get("secure", False)),
    }


def cookies_to_json(cookies: list, *, escaped: bool = True) -> str:
    """序列化为单行 JSON。

    escaped=True 必须用于写进 .env 的场景：utils/config.py 读取时会做
    `.encode("utf-8").decode("unicode_escape")`，字面中文会被这步毁成乱码，
    转成 \\uXXXX 才能安全往返。
    """
    if escaped:
        return json.dumps(cookies, ensure_ascii=True, separators=(",", ":"))
    return json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))


def parse_cookies(text: str):
    """把 cookies 文本解析成列表；非法返回 (None, 错误信息)。"""
    text = (text or "").strip()
    if not text:
        return None, "Cookies 为空"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"不是合法 JSON：{exc.msg}"
    if not isinstance(data, list):
        return None, "必须是 JSON 数组（形如 [{...}]）"
    return data, ""


# ---------------------------------------------------------------------------
# 账号信息：拦截 SELF_PROFILE_API 的响应，取 nickname / unique_id
# ---------------------------------------------------------------------------
# 注入到页面世界的钩子。它只做「记录」，不主动发请求，也不改动请求参数。
#
# 为什么不用 page.route 改写请求：抖音的请求带 a_bogus 签名，重放风险高。
# 为什么同时还要 Python 侧 page.on("response")：两条路互补 ——
#   页面钩子不依赖 Python 事件泵，但注入痕迹留在页面世界里；
#   Python 侧监听不注入任何代码，但读到 body 的时机受事件派发影响。
# 两条都挂上，谁先拿到就用谁。
_INTERCEPT_JS = r"""
(() => {
  if (window.__dySelfHook) return;
  const hook = { profile: null, status: null, hits: [] };
  window.__dySelfHook = hook;

  const match = (url) => String(url || "").indexOf("/user/profile/self") >= 0;

  const record = (text, via) => {
    if (!text || text.charAt(0) !== "{") return;
    let data;
    try { data = JSON.parse(text); } catch (e) { return; }
    if (!data || typeof data !== "object") return;

    // 无论有没有 user 都记一份状态，方便上层区分「没截到」和「截到了但未登录」
    hook.status = {
      code: data.status_code,
      msg: data.status_msg != null ? String(data.status_msg) : "",
      via: via,
      at: Date.now()
    };

    const u = data.user;
    if (!u || typeof u !== "object") return;
    if (!u.nickname && !u.unique_id && !u.short_id) return;

    hook.profile = data;
    hook.hits.push(via + "@" + Date.now());
    if (hook.hits.length > 20) hook.hits.shift();
  };

  const _fetch = window.fetch;
  if (typeof _fetch === "function") {
    window.fetch = function (...args) {
      let url = "";
      try { url = (args[0] && args[0].url) || String(args[0] || ""); } catch (e) {}
      const promise = _fetch.apply(this, args);
      if (match(url)) {
        try {
          promise.then((r) => r.clone().text()).then((t) => record(t, "fetch")).catch(() => {});
        } catch (e) {}
      }
      return promise;
    };
  }

  const proto = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
  if (proto) {
    const _open = proto.open;
    const _send = proto.send;
    proto.open = function (method, url, ...rest) {
      try { this.__dyUrl = String(url || ""); } catch (e) {}
      return _open.call(this, method, url, ...rest);
    };
    proto.send = function (...args) {
      try {
        if (match(this.__dyUrl)) {
          this.addEventListener("load", () => {
            try { record(this.responseText, "xhr"); } catch (e) {}
          });
        }
      } catch (e) {}
      return _send.apply(this, args);
    };
  }
})();
"""

# 最后兜底：扫 localStorage 里可能存在的用户信息（接口完全没截到时才用）
_LOCAL_STORAGE_JS = r"""
() => {
  const pick = (u) => {
    if (!u || typeof u !== "object") return null;
    const uid = u.unique_id || u.short_id;
    const nick = u.nickname || u.nick_name || u.name;
    if (!nick && !uid) return null;
    return { nickname: nick ? String(nick) : "", unique_id: uid ? String(uid) : "" };
  };
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key || !/user|profile|account|login/i.test(key)) continue;
      const raw = localStorage.getItem(key);
      if (!raw || (raw.charAt(0) !== "{" && raw.charAt(0) !== "[")) continue;
      let data;
      try { data = JSON.parse(raw); } catch (e) { continue; }
      for (const cand of [data, data && data.user, data && data.userInfo,
                          data && data.user_info, data && data.data, data && data.profile]) {
        const got = pick(cand);
        if (got && (got.nickname || got.unique_id)) {
          return Object.assign(got, { source: "localStorage:" + key });
        }
      }
    }
  } catch (e) {}
  return null;
}
"""


def extract_self_profile(data) -> dict:
    """从 self profile 响应里取出账号信息（纯函数，便于单测）。

    真实响应结构（2026-09 实测）::

        {
          "status_code": 0,
          "status_msg": null,
          "user": {"nickname": "示例昵称", "unique_id": "12345678901",
                   "short_id": "12345678901", "uid": "10000000000000001", ...}
        }

    nickname  -> 用户名
    unique_id -> 抖音号
    """
    out = {
        "nickname": "",
        "unique_id": "",
        "short_id": "",
        "uid": "",
        "sec_uid": "",
        "id_source": "",
        "status_code": None,
        "status_msg": "",
        "source": "",
    }
    if not isinstance(data, dict):
        return out

    out["status_code"] = data.get("status_code")
    out["status_msg"] = str(data.get("status_msg") or "")

    user = data.get("user")
    if not isinstance(user, dict):
        return out

    def text(key: str) -> str:
        value = user.get(key)
        return "" if value is None else str(value).strip()

    out["nickname"] = text("nickname") or text("other_nickname")
    out["short_id"] = text("short_id")
    out["uid"] = text("uid")
    out["sec_uid"] = text("sec_uid")

    unique_id = text("unique_id")
    if unique_id:
        out["unique_id"] = unique_id
        out["id_source"] = "unique_id"
    elif out["short_id"]:
        # 用户没设自定义抖音号时，抖音界面展示的「抖音号」就是 short_id
        out["unique_id"] = out["short_id"]
        out["id_source"] = "short_id（该账号未设自定义抖音号）"

    return out


def read_page_hook(page) -> dict:
    """读取页面侧钩子的原始记录：{'profile': 响应, 'status': {...}, 'hits': [...]}。"""
    if page is None:
        return {}
    try:
        got = page.evaluate("() => (window.__dySelfHook || null)")
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def detect_account_info(page) -> dict:
    """仅从页面侧钩子读取并归一化（保留给单独使用页面的场景）。"""
    hook = read_page_hook(page)
    return extract_self_profile(hook.get("profile"))


# ---------------------------------------------------------------------------
# 会话列表：整体委托给 core/douyin_im.py
# ---------------------------------------------------------------------------
# 滚动逻辑只在主程序的 core/douyin_im 里实现一份，本文件不镜像它的选择器。
# 下面几个常量是喂给 DouyinIM 的入参（语义与 utils/config.py 里同名项一致）。
CONVERSATION_READY_TIMEOUT_SECONDS = 45.0  # 门禁等待：登录态 + 会话列表就绪
CONVERSATION_SCAN_TIMEOUT_SECONDS = 180.0  # 滚动扫描总预算（秒）
CONVERSATION_SETTLE_MS = 800               # 每批新会话等的资料静默窗（毫秒）
CONVERSATION_MAX_STEPS = 400               # 滚动步数硬上限
CONVERSATION_PROGRESS_EVERY = 5            # 每收满这么多条就报一次进度
CONVERSATION_HEARTBEAT_SECONDS = 10.0      # 长时间没有新会话时的心跳间隔


def _display_name(item: dict) -> str:
    """把 DouyinIM 给出的会话项折成显示名（display 已是「备注 > 昵称 > 标题」）。"""
    return str(item.get("display") or item.get("title") or "").strip()


def _ready_error(res: dict) -> str:
    """把 DouyinIM 的门禁结论翻译成给用户看的话。"""
    status = res.get("status")
    if status == douyin_im.STATUS_LOGGED_OUT:
        return (
            "这个账号的浏览器配置里没有登录态。\n\n"
            "请先点「刷新登录信息」重新登录一次，再来拉取会话列表。"
        )
    if status == douyin_im.STATUS_EXPIRED:
        return (
            "登录态已失效（本地还存着 sessionid，但服务端已经不认了）。\n\n"
            "请点「刷新登录信息」重新扫码登录，再来拉取会话列表。"
        )
    if status == douyin_im.STATUS_LOGIN_LOST:
        return "拉取过程中登录失效了，请重新登录后再试。"
    if status == douyin_im.STATUS_TIMEOUT:
        return (
            f"等了 {CONVERSATION_READY_TIMEOUT_SECONDS:.0f} 秒也没等到会话列表就绪。\n\n"
            "常见原因：网络太慢，或者抖音改了页面结构。\n"
            "可以先勾选「显示浏览器窗口」重试，看看究竟停在哪一步。"
        )
    return f"没能拿到会话列表（{status}）。\n\n可以先勾选「显示浏览器窗口」重试看看。"


# ---------------------------------------------------------------------------
# 工作线程
# ---------------------------------------------------------------------------
class BrowserLoginWorker(threading.Thread):
    """独占一个 cloakbrowser 实例的后台线程。"""

    def __init__(
        self,
        profile_dir,
        events: queue.Queue | None = None,
        *,
        headless: bool = False,
        fingerprint: str = "",
        proxy: dict | None = None,
    ) -> None:
        super().__init__(daemon=True, name="browser-login-worker")
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        # 该账号固定的指纹种子；空串表示不干预，用 cloakbrowser 的默认随机种子
        self.fingerprint = str(fingerprint or "").strip()
        # 云函数代理配置（local_settings.proxy_config() 那一份）；未启用表示直连
        self.proxy = dict(proxy or {})
        # 本次会话的 gost 隧道：开浏览器前拉起，关浏览器后释放
        self.tunnel = None
        self.commands: queue.Queue = queue.Queue()
        self.events: queue.Queue = events or queue.Queue()
        self.ctx = None
        self.page = None
        # core.douyin_im 的只读监听：判定登录态要用它（它收的是抖音自己的响应）
        self.mon = None
        # 账号信息拦截状态（只在工作线程里读写）
        self.self_profile = None  # Python 侧截到的响应原文
        self.self_status = None  # 最近一次该接口的 {code, msg}，未登录时也有值
        self.self_error = ""  # 读取 body 失败的原因，仅用于排查
        self._interceptors_ready = False
        # 打开的是自定义页面（回归脚本 / 排错）而非抖音聊天页
        self.custom_page = False

    # -- 对外接口 -----------------------------------------------------------
    def send(self, command: str, payload=None) -> None:
        self.commands.put((command, payload))

    def emit(self, kind: str, payload=None) -> None:
        self.events.put((kind, payload))

    def log(self, text: str) -> None:
        self.emit("log", text)

    def is_running(self) -> bool:
        """浏览器是否仍存活（用户可能手动关掉了窗口）。"""
        if self.ctx is None:
            return False
        try:
            self.ctx.cookies()
            return True
        except Exception:
            return False

    # -- 主循环 -------------------------------------------------------------
    def run(self) -> None:
        launch = load_cloakbrowser()
        if launch is None:
            self.emit("error", environment_hint() or f"cloakbrowser 不可用（{IMPORT_ERROR}）")
            self.emit("done")
            return

        while True:
            command, payload = self.commands.get()
            try:
                if command == "open":
                    self._open(payload)
                elif command == "probe":
                    self._probe()
                elif command == "grab":
                    self._grab(payload)
                elif command == "conversations":
                    self._conversations()
                elif command == "shutdown":
                    self._close()
                    break
            except Exception as exc:
                self.emit("error", f"{type(exc).__name__}: {exc}")
        # 线程收尾：不管怎么退出，都别把 gost 留在后台
        self._stop_tunnel()
        self.emit("done")

    # -- 内部实现 -----------------------------------------------------------
    def _reset(self) -> None:
        self.ctx = None
        self.page = None
        self.mon = None
        self.self_profile = None
        self.self_status = None
        self.self_error = ""
        self._interceptors_ready = False
        self.custom_page = False

    # -- 登录态判定（借 core/douyin_im） -------------------------------------
    def _login_verdict(self, *, deep: bool = False) -> dict:
        """登录态判定，复用 ``core.douyin_im.check_login``。

        不参与「能不能开始抓」的决定（那个门禁是 ``_has_login_cookie``）：
        check_login 的 DOM 兜底只要求页面上有 ``[data-e2e="msg-input"]``，
        登录过程中的壳页面也满足，据此触发抓取会打断用户正在进行的登录。

        默认只读 ImMonitor 已收到的 SSR；``deep=True`` 才允许序列化整页 DOM。
        """
        ssr = dict(getattr(self.mon, "login", None) or {})

        # 与 core/douyin_im.check_login 的口径完全对齐：logged_in 还必须带 user_id
        if ssr.get("verdict") == "logged_in" and ssr.get("user_id"):
            return {
                "state": "LOGGED_IN",
                "user_id": str(ssr["user_id"]),
                "nickname": str(ssr.get("nickname") or ""),
                "sec_uid": str(ssr.get("sec_uid") or ""),
                "log": "[LOGIN] ✅ 已登录（SSR）",
            }

        if ssr.get("verdict") == "logged_out":
            # SSR 说未登录，但本地还留着 sessionid → 服务端已经把它作废了
            expired = self._has_login_cookie()
            return {
                "state": "EXPIRED" if expired else "NOT_LOGGED_IN",
                "user_id": "",
                "nickname": "",
                "sec_uid": "",
                "log": "[LOGIN] "
                + ("⚠️ 登录已失效（SSR 说未登录，但本地有 sessionid）"
                   if expired else "⛔ 未登录（SSR）"),
            }

        if not deep:
            return {
                "state": "UNKNOWN",
                "user_id": "",
                "nickname": "",
                "sec_uid": "",
                "log": "[LOGIN] ❓ 还没拿到结论",
            }

        try:
            return douyin_im.check_login(self.page, self.mon)
        except Exception as exc:
            self.log(f"登录态判定出错（按未知处理）：{type(exc).__name__}: {exc}")
            return {
                "state": "UNKNOWN",
                "user_id": "",
                "nickname": "",
                "sec_uid": "",
                "log": "[LOGIN] ❓ 判定失败",
            }

    # -- 账号信息拦截 -------------------------------------------------------
    def _attach_interceptors(self) -> None:
        """在页面导航之前挂上两层拦截，之后抖音自己的请求就会被记录下来。

        必须早于 goto：标签页一旦开始加载，抖音自己的脚本就会立刻发出请求，
        这时候再挂监听就晚了。另外 add_init_script 只对「之后的导航」生效，
        所以同样要放在 goto 之前。
        """
        if self._interceptors_ready or self.page is None:
            return

        try:
            self.page.add_init_script(_INTERCEPT_JS)
        except Exception as exc:
            self.log(f"页面钩子注入失败（不影响使用，会走其它途径）：{type(exc).__name__}: {exc}")

        try:
            self.page.on("response", self._on_response)
        except Exception as exc:
            self.log(f"响应监听注册失败：{type(exc).__name__}: {exc}")

        self._interceptors_ready = True

    def _on_response(self, response) -> None:
        """Playwright 响应回调，在工作线程里同步执行。

        只挑目标接口，并且只读一次 body（读 body 会走一次协议往返，别滥用）。
        这里读失败是正常现象（响应体已被丢弃、被重定向等），记录原因即可。
        """
        try:
            url = response.url
        except Exception:
            return
        if SELF_PROFILE_API not in url:
            return

        try:
            data = response.json()
        except Exception as exc:
            self.self_error = f"{type(exc).__name__}: {exc}"
            return

        if not isinstance(data, dict):
            return

        self.self_error = ""  # 这次读到了，清掉上一次的失败记录
        self.self_status = {
            "code": data.get("status_code"),
            "msg": str(data.get("status_msg") or ""),
        }
        user = data.get("user")
        if isinstance(user, dict) and (
            user.get("nickname") or user.get("unique_id") or user.get("short_id")
        ):
            self.self_profile = data

    def _collect_captured(self) -> dict:
        """把两层拦截的结果归一化成统一结构。

        注意顺序：先做一次 page.evaluate 读页面钩子 —— 这次调用会顺带把排队中的
        响应回调派发掉（工作线程大多时间阻塞在 Queue.get 上，Playwright 事件不会
        自行执行）。读完之后再检查 Python 侧缓存，就不会漏掉刚刚到达的响应。
        """
        hook = read_page_hook(self.page)

        # 先把页面侧记录到的状态补进来。这样不管最终是从哪条路拿到账号，
        # 界面都能拿到 status_code 去判断「已登录」还是「用户未登录」。
        if self.self_status is None and isinstance(hook.get("status"), dict):
            self.self_status = {
                "code": hook["status"].get("code"),
                "msg": str(hook["status"].get("msg") or ""),
            }

        if self.self_profile is not None:
            got = extract_self_profile(self.self_profile)
            if got["nickname"] or got["unique_id"]:
                got["source"] = "接口拦截（响应监听）"
                return got

        if hook.get("profile"):
            got = extract_self_profile(hook["profile"])
            if got["nickname"] or got["unique_id"]:
                got["source"] = "接口拦截（页面钩子）"
                return got

        return extract_self_profile(None)

    def _wait_for_capture(self, seconds: float) -> dict | None:
        """在给定时间内轮询拦截缓存，命中就返回结果，超时返回 None。"""
        deadline = time.monotonic() + seconds
        while True:
            result = self._collect_captured()
            if result["nickname"] or result["unique_id"]:
                return result
            if time.monotonic() >= deadline:
                return None
            try:
                # 必须用 Playwright 的等待，它会派发事件（响应回调才能跑）
                self.page.wait_for_timeout(500)
            except Exception:
                return None

    def _reload_for_profile(self) -> None:
        """两条拦截都空时，刷新一次页面让抖音自己再发一次该请求。"""
        self.log("页面上没截到账号信息接口，刷新一次页面重试…")
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            self.log(f"刷新失败：{type(exc).__name__}: {exc}")
            return

        deadline = time.monotonic() + PROFILE_WAIT_SECONDS
        while time.monotonic() < deadline:
            # 必须用 Playwright 自己的等待：它会派发事件，响应回调才有机会执行。
            # 换成 time.sleep 的话回调永远不会被调用。
            try:
                self.page.wait_for_timeout(500)
            except Exception:
                return
            if self.self_profile is not None:
                return
            try:
                if read_page_hook(self.page).get("profile"):
                    return
            except Exception:
                return

    def _scan_local_storage(self) -> dict:
        if self.page is None:
            return extract_self_profile(None)
        try:
            got = self.page.evaluate(_LOCAL_STORAGE_JS)
        except Exception:
            return extract_self_profile(None)
        if not isinstance(got, dict):
            return extract_self_profile(None)
        result = {
            "nickname": str(got.get("nickname") or ""),
            "unique_id": str(got.get("unique_id") or ""),
            "short_id": "",
            "uid": "",
            "sec_uid": "",
            "id_source": "",
            "status_code": None,
            "status_msg": "",
            "source": str(got.get("source") or ""),
        }
        return result

    def read_account_info(
        self, *, trigger: bool = True, grace: float | None = None, allow_reload: bool = True
    ) -> dict:
        """分层获取账号信息，拿不到就返回空值（绝不抛异常）。

        第 1 层：拦截 `/aweme/v1/web/user/profile/self` 的响应
        第 2 层：原地等一小会儿（请求通常只比 goto 晚几百毫秒）
        第 3 层：刷新页面，等抖音自己再请求一次 —— 受 allow_reload 控制
        第 4 层：扫 localStorage

        自动流程必须传 allow_reload=False：用户可能正在扫码 / 等验证码，
        刷新会打断他的登录流程。
        """
        result = self._collect_captured()
        if result["nickname"] or result["unique_id"]:
            return result

        if not trigger:
            return result

        if self.is_running():
            result = self._wait_for_capture(
                PROFILE_GRACE_SECONDS if grace is None else grace
            )
            if result:
                return result

            if allow_reload:
                self._reload_for_profile()
                result = self._collect_captured()
                if result["nickname"] or result["unique_id"]:
                    return result
            else:
                self.log(
                    "没截到账号信息；当前不允许自动刷新页面（怕打断正在进行的登录）"
                    " —— 请在浏览器窗口里按 F5 刷新一次，之后会自动重试"
                )
        else:
            result = self._collect_captured()

        fallback = self._scan_local_storage()
        if fallback["nickname"] or fallback["unique_id"]:
            return fallback

        return result

    # -- 自动流程的轮询探针 -------------------------------------------------
    def _probe(self) -> None:
        """廉价地看一眼当前状态，供界面的自动流程轮询。

        只读不改：不刷新页面、不等待、不抓 Cookie，所以 1.5 秒探一次也不会
        打扰用户。顺带读一次 Python 侧缓存，把排队中的响应回调派发掉
        （工作线程平时阻塞在 Queue.get 上，Playwright 事件不会自己跑）。
        """
        if not self.is_running():
            self._reset()
            self.emit(
                "probe",
                {
                    "running": False,
                    "logged_in": False,
                    "login_state": "",
                    "detected": extract_self_profile(None),
                    "api_status": None,
                    "url": "",
                },
            )
            return

        # 门禁只看 Cookie（要存进 .env 的就是它），页面级信号可能说谎（见 _login_verdict）
        logged_in = self._has_login_cookie()
        verdict = self._login_verdict()

        detected = (
            self._collect_captured() if logged_in else extract_self_profile(None)
        )
        # 抖音号只有接口给得出来；昵称可先用库的结论兜底
        if not detected.get("nickname") and verdict.get("nickname"):
            detected["nickname"] = verdict["nickname"]

        url = ""
        try:
            url = self.page.url
        except Exception:
            pass

        self.emit(
            "probe",
            {
                "running": True,
                "logged_in": logged_in,
                "login_state": verdict.get("state") or "",
                "detected": detected,
                "api_status": self.self_status,
                "url": url,
            },
        )

    # -- 会话列表（委托 core/douyin_im.DouyinIM） ----------------------------
    def _conversations(self) -> None:
        """枚举会话列表（工作线程内执行）。

        打开聊天页、门禁、滚动枚举都交给 core.douyin_im.DouyinIM，本方法只把进度
        翻译成界面事件、把结论发出去。DouyinIM 会自己再导航一次聊天页（它的契约是
        先挂钩子再导航），这一趟重复加载是有意的。
        """
        if not self.is_running():
            self._reset()
            self.emit("error", "浏览器未运行（可能已被手动关闭），请重试")
            return

        self.emit("status", "正在打开聊天页并检查登录态…")
        try:
            im = douyin_im.DouyinIM(
                self.page,
                timeout=CONVERSATION_SCAN_TIMEOUT_SECONDS,
                max_steps=CONVERSATION_MAX_STEPS,
                settle_ms=CONVERSATION_SETTLE_MS,
                ready_timeout=CONVERSATION_READY_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            self.emit("error", f"会话扫描初始化失败：{type(exc).__name__}: {exc}")
            return

        res = im.wait_ready()
        if res.get("status") != douyin_im.STATUS_READY:
            self.emit("error", _ready_error(res))
            return

        self.log(
            f"门禁通过：user_id={res.get('user_id') or '-'} "
            f"nickname={res.get('nickname') or '-'}"
        )
        self.emit("status", "正在滚动加载全部会话…")

        names: list = []
        seen: set = set()
        reported = 0
        started = time.monotonic()
        last_beat = started
        try:
            for item in im.iter_conversations():
                name = _display_name(item)
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)

                # 进度节流：攒够 N 个报一次，长时间无新增时按心跳报，避免界面像卡死
                now = time.monotonic()
                grew = len(names) != reported
                if (grew and len(names) - reported >= CONVERSATION_PROGRESS_EVERY) or (
                    now - last_beat >= CONVERSATION_HEARTBEAT_SECONDS
                ):
                    self.emit(
                        "conversation_progress",
                        {"count": len(names), "waiting": not grew},
                    )
                    reported = len(names)
                    last_beat = now
        except Exception as exc:
            self.log(f"枚举会话时出错（用已经拿到的部分）：{type(exc).__name__}: {exc}")

        elapsed = round(time.monotonic() - started, 1)
        scan = im.last_scan or {}

        # 折叠组 / 陌生人组是独立滚动容器，主列表扫不到（core/tasks.py 也会提醒）
        try:
            folds = im.fold_groups() or {}
            hidden = sum(len(v or []) for v in folds.values())
        except Exception:
            hidden = 0
        if hidden:
            self.log(f"注意：折叠组 / 陌生人组里还有 {hidden} 个会话，主列表扫不到，未计入")

        try:
            im.detach()
        except Exception:
            pass

        stats = {
            "rounds": scan.get("steps"),
            "elapsed": elapsed,
            "hit_bottom": bool(scan.get("scanned_all")),
            "visited": scan.get("visited"),
            "stopped": scan.get("stopped"),
        }
        self.emit("conversations", {"names": names, "stats": stats})

    def _open(self, payload=None) -> None:
        """打开浏览器并跳到聊天页。

        payload 可以是配置目录，也可以是
        ``{"profile_dir": ..., "ready_status": "...", "url": "..."}`` ——
        ready_status 用来让「打开即抓账号信息」和「打开即拉会话列表」两条流程
        各自给出合适的就绪提示；url 覆盖默认的抖音聊天页（回归脚本拿本地固定
        页面核对选择器/滚动逻辑时会用到）。
        """
        profile_dir = None
        ready_status = ""
        url = ""
        if isinstance(payload, dict):
            profile_dir = payload.get("profile_dir")
            ready_status = str(payload.get("ready_status") or "")
            url = str(payload.get("url") or "")
        elif payload:
            profile_dir = payload

        if profile_dir:
            self.profile_dir = Path(profile_dir)

        if self.is_running():
            self.log("浏览器已在运行，切回聊天页")
            try:
                self.page.bring_to_front()
            except Exception:
                pass
            self.emit("opened")
            return

        self._reset()
        self.emit("status", "正在启动隐身浏览器…")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.log(f"配置目录：{self.profile_dir}")
        if BROWSER_MISSING:
            self.log(f"注意：未找到自带浏览器，尝试使用默认路径 —— {BROWSER_BINARY}")
        else:
            self.log(f"浏览器：{BROWSER_BINARY}")

        launch = load_cloakbrowser()
        # 先把隧道拉起来再开浏览器：隧道起不来宁可不开 —— 否则会拿本机 IP 去登录，
        # cookie 的出口和云端任务对不上，反而给账号添风险。
        proxy_url = self._start_tunnel()
        if self.proxy.get("enabled") and not proxy_url:
            self.emit("error", "配套代理未就绪，已中止打开浏览器（避免用本机 IP 登录）")
            return

        # 显式把指纹钉死：cloakbrowser 默认每次启动随机一个新种子，
        # 传进去的同名 flag 会覆盖它（其余隐身参数不受影响）。
        extra_args = [f"{FINGERPRINT_FLAG}{self.fingerprint}"] if self.fingerprint else []
        if extra_args:
            self.log(f"固定指纹：{FINGERPRINT_FLAG}{self.fingerprint}")
        else:
            self.log("未指定固定指纹，本次由浏览器自行随机")
        if proxy_url:
            # 走代理时必须让 WebRTC 也报代理出口 IP，否则它会绕过 HTTP 代理暴露本机真实 IP
            extra_args.append("--fingerprint-webrtc-ip=auto")

        try:
            self.ctx = launch(
                str(self.profile_dir),
                headless=self.headless,
                proxy=proxy_url or None,
                args=extra_args or None,
            )
        except Exception:
            # 浏览器没起来，这条隧道的使命也就结束了，立刻释放
            self._stop_tunnel()
            raise
        self.log("隐身 Chromium 已启动" + ("（无头模式）" if self.headless else ""))

        # 统一放宽导航超时，让 DouyinIM 内部不传 timeout 的 goto 也受益
        # （Playwright 默认只有 30s，走代理时几乎必超）
        try:
            self.ctx.set_default_navigation_timeout(GOTO_TIMEOUT_MS)
        except Exception as exc:
            self.log(f"设置导航超时失败（用各自默认值继续）：{type(exc).__name__}: {exc}")

        pages = [p for p in self.ctx.pages if not p.is_closed()]
        self.page = pages[0] if pages else self.ctx.new_page()

        # 下面两个钩子都**必须早于 goto**（这是 core/douyin_im 的契约）：
        #   ImMonitor           登录态就写在聊天页首屏的 SSR HTML 里，挂晚了就漏，
        #                       之后只能退回去序列化整个 DOM 才能判定
        #   _attach_interceptors 抖音自己的脚本在页面加载时就会请求账号信息接口
        self.mon = douyin_im.ImMonitor(self.page)
        self._attach_interceptors()

        target_url = url or CHAT_URL
        self.custom_page = bool(url)
        self.log(f"跳转 {target_url}")
        self.emit("status", "正在打开抖音聊天页…（走配套代理，慢的时候要等一会儿）")
        try:
            self.page.goto(
                target_url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS
            )
        except Exception as exc:
            # 超时 ≠ 导航失败：Playwright 只中止「等待」，请求仍在继续，页面随后可能就绪。
            # 这里按致命错误收场的话 opened 事件发不出去，探针就永远不启动。
            reason = " ".join(str(exc).split())      # 异常里带多行 Call log，压成一行
            self.log(
                f"首屏加载等待超时（{GOTO_TIMEOUT_MS // 1000}s）——"
                f"导航仍在继续，继续等页面就绪：{reason}"
            )

        if self.custom_page:
            # 自定义页面（回归脚本 / 排错），不做登录态判断
            pass
        elif self._has_login_cookie():
            self.log("检测到本地已存在的登录态，会自动抓取")
        else:
            self.log("请在浏览器里完成登录（扫码 / 短信验证码）")

        self.emit("opened")
        self.emit(
            "status",
            ready_status or "浏览器已打开 —— 登录成功后会自动抓取并保存",
        )

    def _has_login_cookie(self) -> bool:
        try:
            return any(c.get("name") in LOGIN_COOKIE_NAMES for c in self.ctx.cookies())
        except Exception:
            return False

    def _grab(self, payload=None) -> None:
        """抓取登录信息。

        payload 两个开关默认都关，由 login_dialog 按场景打开：
        allow_reload 允许刷新页面；deep_login 允许跑 check_login 的完整判定。
        """
        options = payload if isinstance(payload, dict) else {}
        allow_reload = bool(options.get("allow_reload"))
        deep_login = bool(options.get("deep_login"))

        if not self.is_running():
            self._reset()
            self.emit("error", "浏览器未运行（可能已被手动关闭），请先点『打开浏览器』")
            return

        self.emit("status", "正在读取登录信息…")

        state = self.ctx.storage_state()
        raw = state.get("cookies", [])

        # 逐条裁剪，丢掉 clean_cookie 判定不可用的（name/domain 为空）——
        # 留着会让 Playwright 的 add_cookies 整批失败，表现为「配置里有 cookie 却登不上」。
        target_raw = [c for c in raw if matches_target(c.get("domain", ""))]
        cookies = [c for c in (clean_cookie(item) for item in target_raw) if c]
        invalid = len(target_raw) - len(cookies)

        # 兜底：一条都没命中、或命中数不到总数一半 —— 都当成「TARGET_DOMAINS 没跟上
        # 站点变化」处理，宁可多带（全量保存）也不要漏：少一条登录态 cookie 就登不上了，
        # 而多带几条无关域的 cookie 顶多是配置大一点。
        if not cookies or len(cookies) * 2 < len(raw):
            if cookies:
                self.log(f"目标域只匹配到 {len(cookies)}/{len(raw)} 条 Cookie，改为保存全部")
            else:
                self.log("未匹配到抖音域下的 Cookie，改为保存全部 Cookie")
            cookies = [c for c in (clean_cookie(item) for item in raw) if c]

        if invalid:
            self.log(f"已剔除 {invalid} 条无效 Cookie（name 或 domain 为空）")

        local_storage: dict[str, dict] = {}
        for origin in state.get("origins", []):
            host = origin.get("origin", "").split("//")[-1].split("/")[0]
            if matches_target(host):
                local_storage.setdefault(origin["origin"], {}).update(
                    origin.get("localStorage", {})
                )

        try:
            user_agent = self.page.evaluate("() => navigator.userAgent")
        except Exception:
            user_agent = ""

        info = self.read_account_info(allow_reload=allow_reload)
        # 放在 read_account_info 之后：允许刷新时它会刷新页面，
        # 刷新后重新收到的 SSR 才是当下的登录态。
        verdict = self._login_verdict(deep=deep_login)

        self.emit(
            "grabbed",
            {
                "cookies": cookies,
                # 「抓到的 cookie 里有没有 sessionid」—— 决定这份 Cookie 存下来有没有用
                "logged_in": any(c["name"] in LOGIN_COOKIE_NAMES for c in cookies),
                # 「服务端认不认这个登录态」—— 决定这次抓取该不该算成功
                # （两者都要满足：cookie 里没 sessionid 存了也白存；
                #   有 sessionid 但已失效，存下去只会让主程序跑到一半掉登录）
                "login_state": verdict.get("state") or "",
                "login_log": verdict.get("log") or "",
                # SSR 里带着昵称但没带抖音号；接口没截到时用它兜个底
                "login_nickname": verdict.get("nickname") or "",
                "total": len(raw),
                "user_agent": user_agent,
                "local_storage": local_storage,
                "storage_state": state,
                "detected": info,
                # 诊断信息：让界面能区分「没截到接口」和「接口说未登录」
                "api_status": self.self_status,
                "capture_error": self.self_error,
            },
        )

    def _close(self) -> None:
        if self.ctx is None:
            self._stop_tunnel()
            return
        try:
            self.ctx.close()
        except Exception:
            pass
        self._reset()
        # 浏览器一关就释放隧道：云函数那边的连接断开，实例随即回收、不再计费
        self._stop_tunnel()
        self.log("浏览器已关闭")

    # -- 云函数隧道 ---------------------------------------------------------
    def _start_tunnel(self) -> str:
        """按需拉起 gost 隧道，返回本地代理地址；未启用或失败时返回空串。

        失败不抛异常 —— 原因通过 error 事件发到界面，由调用方决定是否中止。
        """
        proxy = dict(self.proxy or {})
        if not proxy.get("enabled"):
            return ""

        ok, why = local_settings.proxy_ready(proxy)
        if not ok:
            self.emit("error", f"配套代理配置不可用：{why}")
            return ""

        self._stop_tunnel()  # 上一次会话没清干净时兜底
        try:
            tunnel = GostTunnel(
                proxy.get("tunnel", ""),
                proxy.get("user", ""),
                proxy.get("password", ""),
                gost_path=proxy.get("gost_path", ""),
                log=self.log,
            )
            url = tunnel.start()
        except Exception as exc:
            self.emit("error", f"配套代理启动失败：{exc}")
            return ""

        self.tunnel = tunnel
        self.emit("status", f"已接入配套代理：{url}")
        return url

    def _stop_tunnel(self) -> None:
        """释放隧道（幂等）。浏览器关掉后立刻调用，别让云函数那边白烧实例时长。"""
        tunnel = self.tunnel
        self.tunnel = None
        if tunnel is None:
            return
        try:
            tunnel.stop()
        except Exception as exc:
            self.log(f"释放隧道时出错（可忽略）：{type(exc).__name__}: {exc}")
