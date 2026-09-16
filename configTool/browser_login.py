"""cloakbrowser 登录会话工作线程（无 GUI 依赖）。

设计要点：
  - cloakbrowser 底层是 Playwright 同步 API，有线程亲和性，必须在同一个线程里
    完成全部调用。因此本模块把浏览器操作封在一个后台线程里，外部只通过
    「命令队列 + 事件队列」与它交互，绝不跨线程直接碰 page / context。
  - 每个账户传入自己的 profile 目录，登录态相互隔离，可分别重新登录。
  - 本模块属于 configTool 内部，只依赖同目录的 paths.py 与第三方包，
    不引用仓库里其他目录，打包成 exe 后同样可用。

用法：
    worker = BrowserLoginWorker(profile_dir, fingerprint="73841")
    worker.start()
    worker.send("open")           # 启动浏览器并跳转抖音聊天页
    worker.send("probe")          # 廉价探一次：登录了吗 / 账号信息截到了吗（供自动流程轮询）
    worker.send("grab")           # 抓取登录态
    worker.send("conversations")  # 滚动会话列表容器，收集全部会话名
    worker.send("shutdown")       # 关闭浏览器并结束线程

    然后从 worker.events 里取
    ("log"|"status"|"opened"|"probe"|"grabbed"|"conversations"|
     "conversation_progress"|"error"|"done", payload)

fingerprint 是该账号固定的指纹种子（存在 profiles.json 里，见 profile_store.py）。
传空则退回 cloakbrowser 的默认行为 —— 每次启动随机一个新指纹。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path

import local_settings
import paths
from tunnel import GostTunnel

CHAT_URL = "https://www.douyin.com/chat"

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
          "user": {"nickname": "卢瞳", "unique_id": "49709841077",
                   "short_id": "49709841077", "uid": "108672188914", ...}
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
# 会话列表：滚动容器，收集全部会话名
# ---------------------------------------------------------------------------
# 类名与主程序 core/tasks.py 里的常量完全一致（改了要同步两边）：
#     core/tasks.py::CONVERSATION_LIST_SELECTOR  = ".conversationConversationListwrapper"
#     core/tasks.py::CONVERSATION_ITEM_SELECTOR  = ".conversationConversationItemwrapper"
#     core/tasks.py::CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
# 2026-09-16 用真实登录态实测确认：容器 overflow-y:scroll，标题文本就是会话名
# （有备注名时是备注名，与主程序 checkTargetName() 的匹配口径一致）。
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"

# 会话列表等待/滚动节奏
CONVERSATION_LIST_WAIT_SECONDS = 30.0  # 打开聊天页后等容器出现
CONVERSATION_SCROLL_WAIT_MS = 700      # 每次滚动后等多久，让列表把新一批渲染出来
CONVERSATION_MAX_EMPTY_ROUNDS = 6      # 连续这么多轮既无新名字也无新内容 → 判定到底
CONVERSATION_MAX_ROUNDS = 400          # 硬上限，避免异常页面把线程卡死
CONVERSATION_TIMEOUT_SECONDS = 180.0   # 硬超时

# 一次 evaluate 同时完成「读名字」+「往下滚一格」，并把滚动指标带回来。
# 只读 DOM，不点击、不输入，所以不会有任何副作用。
_CONVERSATION_JS = r"""
() => {
  const LIST_SEL = ".conversationConversationListwrapper";
  const TITLE_SEL = ".conversationConversationItemtitle";

  let wrapper = document.querySelector(LIST_SEL);
  if (!wrapper) {
    // 类名万一变了也不至于完全瞎掉：退一步找「同时带 ConversationList 与 wrapper」的容器
    wrapper = document.querySelector('[class*="ConversationList"][class*="wrapper"]');
  }
  if (!wrapper) {
    return { ok: false, reason: "no-wrapper", names: [] };
  }

  const clean = (text) => String(text == null ? "" : text).replace(/\s+/g, " ").trim();

  let nodes = wrapper.querySelectorAll(TITLE_SEL);
  if (!nodes.length) {
    // 退一步：同时含 "Item" 与 "title" 的类名（大小写都认）
    nodes = wrapper.querySelectorAll('[class*="Item"][class*="title"], [class*="Item"][class*="Title"]');
  }

  const names = [];
  for (const node of nodes) {
    const text = clean(node.innerText != null ? node.innerText : node.textContent);
    if (text) names.push(text);
  }

  const before = wrapper.scrollTop;
  const step = Math.max(400, Math.round(wrapper.clientHeight * 0.85));
  wrapper.scrollTop = before + step;
  const after = wrapper.scrollTop;

  return {
    ok: true,
    names: names,
    before: before,
    after: after,
    moved: after !== before,
    atBottom: after + wrapper.clientHeight >= wrapper.scrollHeight - 2,
    scrollHeight: wrapper.scrollHeight,
    clientHeight: wrapper.clientHeight,
  };
}
"""

# 收尾用：滚回顶部（headful 排错时能看到列表开头）
_CONVERSATION_TOP_JS = r"""
() => {
  const w = document.querySelector(".conversationConversationListwrapper");
  if (w) w.scrollTop = 0;
  return !!(w && w.scrollTop === 0);
}
"""

# 容器是否已渲染出来
_CONVERSATION_READY_JS = r"""
() => !!document.querySelector(".conversationConversationListwrapper")
"""


def merge_names(seen: list, seen_set: set, incoming) -> int:
    """把本轮读到的名字并入累积列表（按首次出现顺序去重），返回新增个数。"""
    added = 0
    for raw in incoming or []:
        name = str(raw or "").strip()
        if name and name not in seen_set:
            seen_set.add(name)
            seen.append(name)
            added += 1
    return added


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
                    self._grab()
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
        self.self_profile = None
        self.self_status = None
        self.self_error = ""
        self._interceptors_ready = False
        self.custom_page = False

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

    def read_account_info(self, *, trigger: bool = True, grace: float | None = None) -> dict:
        """分层获取账号信息，拿不到就返回空值（绝不抛异常）。

        第 1 层：拦截 `/aweme/v1/web/user/profile/self` 的响应
        第 2 层：原地等一小会儿（请求通常只比 goto 晚几百毫秒）
        第 3 层：刷新页面，等抖音自己再请求一次
        第 4 层：扫 localStorage
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

            self._reload_for_profile()
            result = self._collect_captured()
            if result["nickname"] or result["unique_id"]:
                return result
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
        打扰用户。顺带做一次 page.evaluate —— 这个调用会把排队中的响应回调
        派发掉（工作线程平时阻塞在 Queue.get 上，Playwright 事件不会自己跑）。
        """
        if not self.is_running():
            self._reset()
            self.emit(
                "probe",
                {
                    "running": False,
                    "logged_in": False,
                    "detected": extract_self_profile(None),
                    "api_status": None,
                    "url": "",
                },
            )
            return

        logged_in = self._has_login_cookie()
        detected = (
            self._collect_captured() if logged_in else extract_self_profile(None)
        )
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
                "detected": detected,
                "api_status": self.self_status,
                "url": url,
            },
        )

    # -- 会话列表 -----------------------------------------------------------
    def _wait_for_conversation_list(self, timeout: float | None = None) -> bool:
        """等会话列表容器渲染出来。用 Playwright 的等待，它会派发事件。"""
        deadline = time.monotonic() + (
            CONVERSATION_LIST_WAIT_SECONDS if timeout is None else timeout
        )
        while time.monotonic() < deadline:
            try:
                if self.page.evaluate(_CONVERSATION_READY_JS):
                    return True
            except Exception:
                return False
            try:
                self.page.wait_for_timeout(500)
            except Exception:
                return False
        return False

    def read_conversation_list(self) -> tuple:
        """滚动会话列表容器，收集全部会话名。

        返回 (名字列表, 统计信息)。名字按首次出现的顺序排列，已去重。

        判定「到底了」用的是三重信号叠加（比只看行数稳）：
          · 这一轮没有读到新名字
          · scrollHeight 没有变大（说明没有在加载新的一批）
          · scrollTop 已经推不动了（滚到底）
        连续 CONVERSATION_MAX_EMPTY_ROUNDS 轮都没有进展才收手 —— 抖音的列表是
        滚动到底部才去请求下一页，中间会出现「滚了但还没数据」的空档，
        收得太快会漏掉最后一批。
        """
        seen: list = []
        seen_set: set = set()
        empty_rounds = 0
        rounds = 0
        prev_height = -1
        started = time.monotonic()
        last: dict = {}

        while rounds < CONVERSATION_MAX_ROUNDS:
            if time.monotonic() - started > CONVERSATION_TIMEOUT_SECONDS:
                self.log("会话列表拉取超时，先用已经拿到的部分")
                break

            rounds += 1
            try:
                state = self.page.evaluate(_CONVERSATION_JS)
            except Exception as exc:
                self.log(f"读取会话列表出错：{type(exc).__name__}: {exc}")
                break

            if not isinstance(state, dict) or not state.get("ok"):
                if rounds == 1:
                    self.log("页面上没找到会话列表容器，可能登录态已失效")
                break

            last = state
            added = merge_names(seen, seen_set, state.get("names"))

            height = int(state.get("scrollHeight") or 0)
            grew = height > prev_height
            prev_height = height

            if added or grew:
                empty_rounds = 0
            else:
                empty_rounds += 1
                if not state.get("moved"):
                    # 滚不动了，加速判定：正常到底时不必再耗满 MAX_EMPTY_ROUNDS 轮
                    empty_rounds += 1

            if added:
                self.emit(
                    "conversation_progress",
                    {"count": len(seen), "rounds": rounds},
                )
            elif rounds % 10 == 0:
                # 长时间没有新增时给个心跳，界面上能看出还在跑
                self.emit(
                    "conversation_progress",
                    {"count": len(seen), "rounds": rounds, "waiting": True},
                )

            if empty_rounds >= CONVERSATION_MAX_EMPTY_ROUNDS:
                break

            try:
                self.page.wait_for_timeout(CONVERSATION_SCROLL_WAIT_MS)
            except Exception:
                break

        # 收尾：滚回顶部（headful 排错时能看到列表开头）
        try:
            self.page.evaluate(_CONVERSATION_TOP_JS)
        except Exception:
            pass

        stats = {
            "rounds": rounds,
            "elapsed": round(time.monotonic() - started, 1),
            "hit_bottom": empty_rounds >= CONVERSATION_MAX_EMPTY_ROUNDS,
            "scroll_height": last.get("scrollHeight"),
            "client_height": last.get("clientHeight"),
        }
        return seen, stats

    def _conversations(self) -> None:
        """会话列表拉取入口（工作线程内执行）。"""
        if not self.is_running():
            self._reset()
            self.emit("error", "浏览器未运行（可能已被手动关闭），请重试")
            return

        if not self.custom_page and not self._has_login_cookie():
            self.emit(
                "error",
                "这个账号的浏览器配置里没有登录态（找不到 sessionid）。\n\n"
                "请先点「刷新登录信息」重新登录一次，再来拉取会话列表。",
            )
            return

        self.emit("status", "正在加载会话列表…")
        if not self._wait_for_conversation_list():
            self.emit(
                "error",
                "没能等到会话列表出现（页面上找不到 "
                f"{CONVERSATION_LIST_SELECTOR}）。\n\n"
                "常见原因：登录态已失效、网络太慢，或者抖音改了页面结构。\n"
                "可以先勾选「显示浏览器窗口」重试，看看到底停在哪一步。",
            )
            return

        self.emit("status", "正在滚动加载全部会话…")
        names, stats = self.read_conversation_list()
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

        pages = [p for p in self.ctx.pages if not p.is_closed()]
        self.page = pages[0] if pages else self.ctx.new_page()

        # 必须在 goto 之前：抖音在页面加载时就会请求账号信息接口
        self._attach_interceptors()

        target_url = url or CHAT_URL
        self.custom_page = bool(url)
        self.log(f"跳转 {target_url}")
        self.page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)

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

    def _grab(self) -> None:
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

        info = self.read_account_info()

        self.emit(
            "grabbed",
            {
                "cookies": cookies,
                "logged_in": any(c["name"] in LOGIN_COOKIE_NAMES for c in cookies),
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
