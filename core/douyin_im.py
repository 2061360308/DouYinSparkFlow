"""core/douyin_im.py
抖音网页版 IM 会话扫描库 —— 传一个 page，内部自己开门、自己滚、自己找人。

设计契约（三句话）
    1. `DouyinIM(page)` 内部立即挂只读监听钩子，然后才 goto 打开聊天页。
       顺序不可颠倒：钩子必须早于导航，否则首屏响应会漏。
    2. `on_ready(cb)` 注册就绪回调。库自己完成「登录校验 + 会话列表就绪」两道门禁，
       终态通过 status 区分，结果对象同一形状（见 ReadyResult）。
    3. `iter_find_and_select(targets)` 从头滚到尾逐个找人。
       **yield 出来的那一刻，该会话已经被选中且 conv_id 已校验过** ——
       调用方拿到就能直接输入、发送，不需要再定位。

不做什么
    - 不模拟人类操作。cloakbrowser 以 `humanize=True` 启动时已自带拟人化，
      本库只负责「点对地方」，不重复加随机延迟。
    - 不自己构造任何请求。全程只读监听，不签名、不重放。
    - 不假设会话列表非虚拟化。列表是虚拟化的（同时渲染约 7-10 个窗口），
      因此采集必须边滚边累积，且「到底」判定不能用条目数。

本文件是**自足**的：解析层（选择器 / 页面内表达式 / protobuf 解码 / SSR 抓取 /
只读监听器 / 登录态判定 / 身份算法）全部内联在此
本文件结构：
    一、选择器          二、页面内表达式      三、protobuf 解码
    四、SSR 登录态      五、身份算法          六、只读监听器 ImMonitor
    七、登录态判定      八、DouyinIM 主体
"""

import time
import traceback
import json
import re
import unicodedata
from urllib.parse import unquote

from utils.config import get_config
from utils.logger import setup_logger

# 本模块的日志器。名字固定 "douyin_im"，与 core.tasks 的 "app" 分开，
# 便于单独按 logger 名过滤。级别取自 .env 的 LOG_LEVEL（utils.config 统一读）。
#
# 注意：这里在**模块导入时**就初始化。utils.config 是纯 env 读取、无网络/无 IO，
# 所以 import 期调用是安全的（它自己的 config 也是这么读的）。
logger = setup_logger("douyin_im", level=get_config().get("logLevel", "Info"))

CHAT_URL = "https://www.douyin.com/chat"

# 会话项行高（CSS 实测 height:67px），用于按 data-index 估算 scrollTop
ROW_HEIGHT = 67

# 会话列表的独立容器。主容器之外还有折叠组与陌生人组，
# 它们各自是独立滚动容器，主容器扫不到 —— 见 DouyinIM.fold_groups。
SEL_LIST_FOLD = ".conversationFoldConversationListlistWrapper"
SEL_LIST_STRANGER = ".conversationStrangerConversationListlistWrapper"

# ===========================================================================
# 一、选择器（全部来自 HAR 里 pcim 打包产物的 CSS/JS 原文）
# ===========================================================================

SEL_ITEM = '[data-e2e="conversation-item"]'          # 会话项官方挂点
SEL_TITLE = ".conversationConversationItemtitle"     # 会话显示名（备注优先）
SEL_LIST = ".conversationConversationListwrapper"    # 列表滚动容器
SEL_CUR = ".conversationConversationItemcurConversation"  # 当前选中态
SEL_LOGIN_BOX = '[data-e2e="login-container"]'       # 未登录时渲染
SEL_AVATAR = '[data-e2e="user-avatar-card"]'         # 登录后头像卡
SEL_MSG_ITEM = '[data-e2e="msg-item-content"]'
SEL_MSG_FROM_ME = ".MessageBoxContentisFromMe"
SEL_SEND_BTN = ".messageMsgInputpublishBtn"
SEL_SEND_BTN_READY = ".messageMsgInputpublishBtn.messageMsgInputpublishRedBtn"  # 有内容可发

# 输入框：优先 contenteditable 本体（humanize 的"可编辑"检查能过），容器兜底
EDITOR_CANDIDATES = (
    '[data-e2e="msg-input"] .public-DraftEditor-content',
    '.DraftEditor-root [contenteditable="true"]',
    '.messageEditorimChatEditorContainer',
)

# ===========================================================================
# 二、页面内执行表达式（page.evaluate 直接跑，返回结构化结果）
# ===========================================================================

JS_LOGIN_DOM = """(() => ({
  loginVisible: !!document.querySelector('[data-e2e="login-container"]'),
  avatarCard: !!document.querySelector('[data-e2e="user-avatar-card"]'),
  hasChatRoot: !!document.querySelector('[data-e2e="msg-input"]')
            || !!document.querySelector('.conversationConversationListwrapper'),
  itemCount: document.querySelectorAll('[data-e2e="conversation-item"]').length,
}))()"""

# 列表"可以开始滚了"（≠ 列表已全量加载）。标题非空、头像已加载、容器有高度。
JS_LIST_READY = """(() => {
  const items = document.querySelectorAll('[data-e2e="conversation-item"]');
  if (items.length === 0) return { ready: false, why: 'no-item', count: 0 };
  let blank = 0;
  items.forEach(el => {
    const t = el.querySelector('.conversationConversationItemtitle');
    if (!t || !t.textContent.trim()) blank++;
  });
  if (blank > 0) return { ready: false, why: 'title-blank', blank, count: items.length };
  const img = items[0].querySelector('img');
  if (img && !img.complete) return { ready: false, why: 'avatar-loading', count: items.length };
  const box = document.querySelector('.conversationConversationListwrapper');
  if (!box || box.clientHeight <= 0) return { ready: false, why: 'no-container', count: items.length };
  return { ready: true, count: items.length };
})()"""

# 采集当前窗口的会话：DOM 字段 + React fiber 里的 conversation 模型。
# 注意：虚拟列表下这里只返回**当前窗口那 7-10 项**，不是全部好友。
JS_COLLECT = """(() => {
  const ITEM = '[data-e2e="conversation-item"]';

  function convOf(el) {
    const keys = Object.keys(el).filter(k => k.startsWith('__reactProps$') || k.startsWith('__reactFiber$'));
    for (const k of keys) {
      if (k.startsWith('__reactProps$')) {
        const p = el[k];
        if (p && p.conversation && (p.conversation.id || p.conversation.conversationId)) return p.conversation;
        continue;
      }
      let f = el[k], d = 0;
      while (f && d++ < 30) {
        const p = f.memoizedProps;
        if (p && p.conversation && (p.conversation.id || p.conversation.conversationId)) return p.conversation;
        f = f.return;
      }
    }
    return null;
  }
  // 标题里的空格被前端 TextKeepSpaces 换成了 U+00A0（源码明文 replace(/ /g,"\\xa0")），
  // 不还原的话标题会带隐形字符，落库/比较都容易踩坑。
  const clean = s => s == null ? null : String(s).replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
  const txt = (el, s) => { const n = el.querySelector(s); return n ? clean(n.textContent) : null; };
  const has = (el, s) => !!el.querySelector(s);
  const safe = fn => { try { return fn(); } catch (e) { return null; } };

  return [...document.querySelectorAll(ITEM)].map((el, index) => {
    const conv = convOf(el);
    const convId = conv ? (conv.id || conv.conversationId || null) : null;
    // toParticipantUserId / toParticipantSecUserId 是 prototype getter（内部拿 this.ctx 比对自己 uid），
    // 包装一层 try：会话被折叠/退出时 getter 可能抛错，不能让整个采集挂掉。
    const uidRaw = conv ? safe(() => conv.toParticipantUserId ?? null) : null;
    // 虚拟列表 item 容器上的 data-index（库代码 jsx("div",{"data-index":e.index})）。
    // 它是「渲染序号」不是身份，重排/虚拟化会变，只作调试与交叉验证用。
    const holder = el.closest('[data-index]');
    return {
      index,
      dataIndex: holder ? Number(holder.getAttribute('data-index')) : null,
      convId,
      type: conv ? (conv.type ?? null) : null,
      uid: (uidRaw === null || uidRaw === undefined) ? null : String(uidRaw),
      secUid: conv ? (conv.toParticipantSecUserId ?? null) : null,
      shortId: conv ? (conv.shortId ?? conv.short_id ?? null) : null,
      participantCount: conv ? (conv.participantCount ?? null) : null,
      title: txt(el, '.conversationConversationItemtitle'),
      desc: txt(el, '.ConversationItemDescwrapper'),
      unread: txt(el, '.ConversationItemUnReadCountim-saas-unreadCountBadge'),
      isCurrent: has(el, '.conversationConversationItemcurConversation'),
      isMuted: has(el, '.ConversationItemTagNextToTitlemuteIcon'),
      isOnline: has(el, '.ConversationItemDesconlineStatus'),
      fromFiber: !!conv,
    };
  });
})()"""

# 滚动状态。三个量各管各的，别混：
#   atBottom  —— 用户是否滚到底（我们要的）
#   notFilled —— 内容还没填满容器（前端用它决定要不要继续补页，不是到底）
JS_SCROLL_PROBE = """(() => {
  const el = document.querySelector('.conversationConversationListwrapper');
  if (!el) return { found: false };
  return {
    found: true,
    scrollTop: Math.round(el.scrollTop),
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
    atBottom: el.scrollTop + el.clientHeight >= el.scrollHeight - 1,
    notFilled: el.scrollHeight <= el.clientHeight + 1,
    count: document.querySelectorAll('[data-e2e="conversation-item"]').length,
  };
})()"""

# 按绝对像素滚动。虚拟列表用 scrollTo 比 scrollBy 可控：
# 外部在 yield 期间可能改变布局，相对位移会累积误差。
JS_SCROLL_TO = """(top) => {
  const el = document.querySelector('.conversationConversationListwrapper');
  if (!el) return false;
  el.scrollTo({ top: top, behavior: 'instant' });
  return true;
}"""

# 记录扫描进度用的最小指纹：只要 data-index 与标题
JS_WINDOW_FINGERPRINT = """(() => {
  const items = [...document.querySelectorAll('[data-e2e="conversation-item"]')];
  const idx = items.map(el => {
    const h = el.closest('[data-index]');
    return h ? Number(h.getAttribute('data-index')) : null;
  }).filter(v => v !== null);
  return { count: items.length, minIndex: idx.length ? Math.min(...idx) : null,
           maxIndex: idx.length ? Math.max(...idx) : null };
})()"""

JS_EDITOR_EMPTY = """(() => {
  const ed = document.querySelector('[data-e2e="msg-input"] .public-DraftEditor-content')
          || document.querySelector('.DraftEditor-root [contenteditable="true"]');
  if (!ed) return null;
  return (ed.textContent || '').trim().length === 0;
})()"""

JS_EDITOR_CLEAR = """(() => {
  const ed = document.querySelector('[data-e2e="msg-input"] .public-DraftEditor-content')
          || document.querySelector('.DraftEditor-root [contenteditable="true"]');
  if (!ed) return false;
  ed.focus();
  const sel = window.getSelection();
  const r = document.createRange();
  r.selectNodeContents(ed);
  sel.removeAllRanges();
  sel.addRange(r);
  document.execCommand('delete');
  return true;
})()"""

JS_CURRENT_CONV = """(() => {
  const el = document.querySelector('.conversationConversationItemcurConversation');
  if (!el) return null;
  const t = el.querySelector('.conversationConversationItemtitle');
  const title = t ? t.textContent.replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim() : null;
  const idx = [...document.querySelectorAll('[data-e2e="conversation-item"]')].indexOf(el);
  let convId = null;
  const keys = Object.keys(el).filter(k => k.startsWith('__reactFiber$'));
  for (const k of keys) {
    let f = el[k], d = 0;
    while (f && d++ < 30) {
      const p = f.memoizedProps;
      if (p && p.conversation && (p.conversation.id || p.conversation.conversationId)) {
        convId = p.conversation.id || p.conversation.conversationId; break;
      }
      f = f.return;
    }
    if (convId) break;
  }
  return { index: idx, title, convId };
})()"""

JS_MSG_STATE = """(() => {
  const items = document.querySelectorAll('[data-e2e="msg-item-content"]');
  const last = items[items.length - 1];
  return {
    count: items.length,
    lastFromMe: last ? !!last.closest('.MessageBoxContentisFromMe') : null,
    lastText: last ? last.textContent.trim().slice(0, 80) : null,
  };
})()"""

# ===========================================================================
# 三、protobuf 最小解码（Python 原生大整数，不存在 JS 的 2^53 精度坑）
# ===========================================================================

SERVICE_NAMES = {
    100: "SEND_MESSAGE", 301: "GET_BY_CONVERSATION", 605: "PARTICIPANTS_LIST",
    610: "GET_CONVERSATION_INFO_LIST", 1001: "GET_STRANGER_LIST",
    2000: "GET_READ_INDEX", 2002: "MARK_READ", 2038: "BATCH_MARK_READ",
    2043: "GET_MESSAGE_BY_INIT", 2048: "GET_USER_MESSAGE",
}


def _read_varint(buf, p):
    """→ (值, 新位置)。字段越界直接抛，调用方按"解到哪算哪"处理。"""
    r = 0
    s = 0
    while True:
        if p >= len(buf):
            raise ValueError("varint overflow")
        b = buf[p]
        p += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80):
            return r, p
        s += 7
        if s > 70:
            raise ValueError("varint too long")


def scan(buf):
    """解一层 protobuf → [(field, wire, value)]；wire=0 时 value 是 int，wire=2 时是 bytes。"""
    out = []
    p = 0
    n = len(buf)
    while p < n:
        try:
            tag, p = _read_varint(buf, p)
        except ValueError:
            break
        field, wire = tag >> 3, tag & 7
        if field == 0:
            break
        try:
            if wire == 0:
                v, p = _read_varint(buf, p)
                out.append((field, wire, v))
            elif wire == 2:
                ln, p = _read_varint(buf, p)
                if p + ln > n:
                    break
                out.append((field, wire, buf[p:p + ln]))
                p += ln
            elif wire == 5:
                if p + 4 > n:
                    break
                p += 4
                out.append((field, wire, None))
            elif wire == 1:
                if p + 8 > n:
                    break
                p += 8
                out.append((field, wire, None))
            else:
                break
        except ValueError:
            break
    return out


def _pick(fs, tag):
    for f in fs:
        if f[0] == tag:
            return f
    return None


def _pick_all(fs, tag):
    return [f for f in fs if f[0] == tag]


def _as_text(sub):
    if not sub:
        return ""
    s = sub.decode("utf-8", errors="replace")
    return "" if "\ufffd" in s else s


def envelope(buf):
    """
    IM 统一回执信封。实测最外层就是信封：1=cmd 2=seq 3=code 4=status 6=业务体 13=自己uid。
    注意：不要先去下钻 f6 再找 code —— 那样读到的 code 是业务体里的。
    """
    r = {"cmd": None, "name": "", "seq": None, "code": None, "status": "", "body": None}
    try:
        fs = scan(buf)
        f = _pick(fs, 1)
        if f and f[1] == 0:
            r["cmd"] = f[2]
            r["name"] = SERVICE_NAMES.get(f[2], f"CMD_{f[2]}")
        f = _pick(fs, 2)
        if f and f[1] == 0:
            r["seq"] = f[2]
        f = _pick(fs, 3)
        if f and f[1] == 0:
            r["code"] = f[2]
        f = _pick(fs, 4)
        if f and f[1] == 2:
            r["status"] = _as_text(f[2])
        f = _pick(fs, 6)
        if f and f[1] == 2:
            r["body"] = f[2]
    except Exception:
        pass
    return r


def _envelope_code(buf):
    """只读返回码（信封字段 3）。8 = 未登录（官方 ERROR_CODE.ERROR_USER_NOT_LOGIN）。"""
    try:
        fs = scan(buf)
    except Exception:
        return None
    f = _pick(fs, 3)
    if f and f[1] == 0:
        return f[2]
    return None


def decode_send_resp(buf):
    """任务三的验收信号。ok = code==0 且 status=='OK'。"""
    env = envelope(buf)
    message_id = None
    server_uuid = None
    self_uid = None
    try:
        if env["body"]:
            inner = _pick(scan(env["body"]), 100)          # f6 → f100
            body = scan(inner[2]) if inner and inner[1] == 2 else []
            f = _pick(body, 1)                             # f100.f1 = message_id(int64)
            if f and f[1] == 0 and f[2] != 0:
                message_id = str(f[2])
            f = _pick(body, 4)                             # f100.f4 = 服务端 UUID
            s = _as_text(f[2]) if f and f[1] == 2 else ""
            if s and re.fullmatch(r"[0-9a-fA-F-]{30,}", s):
                server_uuid = s
        f = _pick(scan(buf), 13)                           # 外层 f13 = 自己 uid
        if f and f[1] == 0 and f[2] and len(str(f[2])) > 4:
            self_uid = str(f[2])
    except Exception:
        pass
    return {
        "ok": env["code"] == 0 and (env["status"] or "").lower() == "ok",
        "code": env["code"], "status": env["status"], "cmd": env["cmd"],
        "message_id": message_id, "server_uuid": server_uuid, "self_uid": self_uid,
    }


def decode_init_resp(buf):
    """任务一的验收信号。complete = has_more is False。"""
    env = envelope(buf)
    has_more = None
    msg_count = 0
    try:
        if env["body"]:
            b = scan(env["body"])
            wrapped = _pick(b, 2043)                       # 兼容被包一层 <2043> 的形态
            if wrapped and wrapped[1] == 2:
                b = scan(wrapped[2])
            f = _pick(b, 2)
            if f and f[1] == 0:
                has_more = (f[2] == 1)
            msg_count = len(_pick_all(b, 1))
    except Exception:
        pass
    return {
        "complete": has_more is False,
        "has_more": has_more, "msg_count": msg_count, "code": env["code"],
    }


def decode_user_info(raw):
    """data[] → 好友资料。remark_name 字段缺失 = 没设备注。"""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return []
    if not isinstance(raw, dict):
        return []
    out = []
    for d in (raw.get("data") or []):
        if not isinstance(d, dict):
            continue
        remark = d.get("remark_name") or ""
        out.append({
            "uid": str(d["uid"]) if d.get("uid") is not None else None,
            "sec_uid": d.get("sec_uid") or None,
            "nickname": d.get("nickname") or "",
            "remark": remark,
            "has_remark": bool(remark),
            "unique_id": d.get("unique_id") or "",
        })
    return out


# ===========================================================================
# 四、SSR 登录态（只能正则扫，SSR 里有裸 $undefined，JSON.parse 必崩）
# ===========================================================================

_VIEWS_FIXERS = (
    lambda s: s,
    lambda s: s.replace('\\"', '"'),
    lambda s: unquote(s),
)


def _take(text, pattern, gate=None):
    m = re.search(pattern, text)
    if not m:
        return None
    v = m.group(1)
    if v in ("$undefined", "undefined", "null"):
        return None
    if gate and not gate(v):
        return None
    return v


def scrape_ssr(html):
    """从 chat 页 SSR HTML 抓登录态。返回 {verdict, user_id, nickname, sec_uid, raw}。"""
    out = {"verdict": "unknown", "user_id": None, "nickname": None, "sec_uid": None, "raw": {}}
    if not html:
        return out
    if isinstance(html, (bytes, bytearray)):
        html = html.decode("utf-8", errors="replace")

    raw = out["raw"]
    for view in _VIEWS_FIXERS:
        try:
            v = view(html)
        except Exception:
            continue
        if not isinstance(v, str) or not v:
            continue

        if not raw.get("odin_user_id"):
            raw["odin_user_id"] = _take(
                v, r'"odin"\s*:\s*"\{\\?"user_id\\?"\s*:\s*\\?"([0-9]{5,25})')
            if not raw["odin_user_id"]:
                od = re.search(r'"odin"\s*:\s*"([^"]{0,400})"', v)
                if od:
                    raw["odin_user_id"] = _take(
                        od.group(1).replace('\\"', '"'), r'"user_id"\s*:\s*"?([0-9]{5,25})')
        if raw.get("is_login") is None:
            raw["is_login"] = _take(v, r'"user"\s*:\s*\{\s*"isLogin"\s*:\s*(true|false)')
        if not raw.get("uid"):
            raw["uid"] = _take(
                v,
                r'"user"\s*:\s*\{\s*"isLogin"\s*:\s*(?:true|false)\s*,\s*"info"\s*:\s*\{\s*"uid"\s*:\s*"([^"]*)"',
                gate=lambda x: bool(re.fullmatch(r"[0-9]{5,25}", x)))
        if not raw.get("sec_uid"):
            raw["sec_uid"] = _take(
                v,
                r'"user"\s*:\s*\{\s*"isLogin"\s*:\s*(?:true|false)\s*,\s*"info"\s*:\s*\{[^}]*?"secUid"\s*:\s*"([^"]+)"')
        if not out["nickname"]:
            out["nickname"] = _take(v, r'"nickname"\s*:\s*"([^"$][^"]{0,60})"')
        if raw.get("not_exist_login_cookie") is None:
            raw["not_exist_login_cookie"] = _take(
                v, r'"not_exist_login_cookie"\s*:\s*(true|false)')

    out["user_id"] = raw.get("odin_user_id") or raw.get("uid")
    out["sec_uid"] = raw.get("sec_uid")
    if out["user_id"] and out["user_id"] != "0":
        out["verdict"] = "logged_in"
    elif raw.get("is_login") == "false" or raw.get("not_exist_login_cookie") == "true":
        out["verdict"] = "logged_out"
    elif not out["user_id"] and raw.get("is_login") is None:
        out["verdict"] = "logged_out"
    return out


# ===========================================================================
# 五、身份算法（会话 id ↔ 对方 uid）与归一化
# ===========================================================================

def _is_group(conv_id, type_, participant_count):
    """
    判断是不是群聊。三种依据，优先级从硬到软：
      ① conv_id 里没有 ":"   → 群/特殊会话（实测群聊 id 是 SDK 分配的纯数字，
         单聊恒为 assembleConvId 产出的 `0:1:<小uid>:<大uid>`）
      ② conv_id 前缀 0:1:    → 单聊
      ③ 成员数 > 2
    都对不上返回 None（未知），不要瞎猜。

    ⚠️ `type_` 参数**不参与判断**：实测 `conv.type` 是长整型会话 id
    （如 7209265235419514xxx），不是类型枚举。签名保留是为了向后兼容。
    """
    s = str(conv_id) if conv_id else ""
    if s:
        if ":" not in s:
            return True
        if s.startswith("0:1:"):
            return False
        if s.startswith("0:2:"):
            return True
    if participant_count and participant_count > 2:
        return True
    return None


def peer_uid_of(conv_id, self_uid):
    """单聊对方 uid：conv_id 形如 `0:1:<uidA>:<uidB>`（数值升序），
    跟自己的 uid 比一下就知道哪个是对方。群聊返回 None。"""
    if not conv_id:
        return None
    p = str(conv_id).split(":")
    if len(p) != 4 or p[1] != "1":
        return None
    return p[3] if str(p[2]) == str(self_uid) else p[2]


def _norm(s):
    """归一化：NFKC + 去 NBSP / 零宽 / 空白 + 小写。

    这是**全项目唯一的归一化实现**（旧 `utils.norm` 已删除并入此处）。
    `config.py` 读取配置时不再归一化，只由 `tasks.py` 在匹配前统一归一 ——
    两边都归过才谈得上相等。
    """
    if s is None:
        return ""
    v = unicodedata.normalize("NFKC", str(s))
    v = v.replace("\u3000", " ").replace("\xa0", " ")
    v = v.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", "", v).strip().lower()


# 公开别名：供 core/tasks.py 等外部模块导入（_norm 是内部名，留着的调用点太多）
norm = _norm


def split_message_lines(text):
    """把消息正文断成「要逐行键入」的段列表。两种换行都认。

    链路上有两种 `\\n`，来自不同源头，必须都处理：

    | 形态 | 例子 | 来源 |
    |---|---|---|
    | **字面** `\\` + `n` | `"甲\\n乙"` | `.env` 的 `MESSAGE_TEMPLATE` —— `dotenv` 读出来**不做 unescape** |
    | **真**换行 `U+000A` | `"甲\\n乙"` | 第三方内容（一言 API 正文）、手写 `.env` 的引号多行值 |

    只认字面 `\\n` 会把真换行**静默吞掉**（三行挤成一行）；只认真换行会让
    `.env` 的默认模板整段变成一行。所以先统一、再断。

    ★ 三条 replace 的顺序**无所谓**（实测可交换）：字面 `\\r\\n` 是 `\\`+`r`+`\\`+`n`
    四个字符，里面没有真的 `U+000D`，所以「真 CRLF → 真 LF」那条规则对字面串
    无从下手；反过来，「字面 `\\r\\n` → 字面 `\\n`」对真 CRLF 也匹配不到。
    **两条规则作用在互不相交的字符集上。** 这里按「先字面、后真换行」写只是为了读着顺。

    空段由调用方的 `if line:` 挡掉（不键入），但**段间仍会按一次 `Shift+Enter`** ——
    因为那确实是两个换行符，视觉上就该空一行。
    """
    return (
        str(text)
        .replace("\\r\\n", "\\n")
        .replace("\r\n", "\n")
        .replace("\\n", "\n")
        .split("\n")
    )


# ---------------------------------------------------------------------------
# 脱敏工具：给自测 / 单测生成「格式合法但非真实」的假身份，
# 避免把真实 uid / 昵称 / 会话 id 写进代码或测试。
# ---------------------------------------------------------------------------

# 固定种子 → 每次运行结果一致，断言可以写死；同时又不是任何真值。
_FAKE_RNG_SEED = 20260919


def fake_uid(rng=None, digits=17):
    """生成形如抖音 uid 的十进制串（真实 uid 为 17 位左右的 int64）。"""
    import random
    r = rng or random.Random(_FAKE_RNG_SEED)
    return str(r.randrange(10 ** (digits - 1), 10 ** digits))


def fake_sec_uid(rng=None, body_len=48):
    """生成形如 `MS4wLjABAAAA…` 的 sec_uid（固定前缀 + 随机 URL-safe 体）。"""
    import random
    import string
    r = rng or random.Random(_FAKE_RNG_SEED)
    alphabet = string.ascii_letters + string.digits + "-_"
    return "MS4wLjABAAAA" + "".join(r.choice(alphabet) for _ in range(body_len))


def fake_conv_id(rng=None, uid_a=None, uid_b=None):
    """生成单聊 conv_id：`0:1:<小uid>:<大uid>`（数值升序，同官方 assembleConvId）。"""
    import random
    r = rng or random.Random(_FAKE_RNG_SEED)
    a = int(uid_a) if uid_a else int(fake_uid(r))
    b = int(uid_b) if uid_b else int(fake_uid(r))
    if b < a:
        a, b = b, a
    return f"0:1:{a}:{b}"


# 自测里用的固定假身份（固定种子派生，写死断言可读）
_FAKE_ME = "10000000000000001"
_FAKE_PEER_A = "10000000000000002"
_FAKE_PEER_B = "10000000000000003"


# ===========================================================================
# 六、监听器：只读旁路，page.on("response") 抓 5 类响应
# ===========================================================================

class ImMonitor:
    """把"网页自己发出去的请求"翻译成三个任务需要的结论。全程只读，不改任何请求。"""

    # 只留真正要用的：登录态、列表完整、好友资料、发送回执、会话基本信息
    ROUTES = (
        ("login", re.compile(r"^https://www\.douyin\.com/(chat|$|\?)")),
        ("send", re.compile(r"imapi\.douyin\.com/v1/message/send")),
        ("init", re.compile(r"/v1/message/get_message_by_init")),
        ("userInfo", re.compile(r"/aweme/v1/web/im/user/info/")),
        ("convInfo", re.compile(r"/v2/conversation/get_info_list")),
    )

    def __init__(self, page):
        self.page = page
        self.login = {"verdict": "unknown", "user_id": None, "nickname": None, "sec_uid": None}
        self.list_rounds = []
        self.list_complete = False
        self.list_has_more = None
        self.peers = {}          # sec_uid|uid → 资料
        self.peer_order = []
        self.sends = []
        self.hits = {}
        self.errors = []
        page.on("response", self._on_response)

    # ---- 内部 ----
    def _on_response(self, resp):
        try:
            url = resp.url
        except Exception:
            return
        route = None
        for rid, rx in self.ROUTES:
            if rx.search(url):
                route = rid
                break
        if route is None:
            return

        try:
            body = resp.body()
        except Exception as e:
            self.errors.append((url, str(e)))
            return
        if not body:
            return

        self.hits[route] = self.hits.get(route, 0) + 1
        try:
            if route == "login":
                self._handle_login(body)
            elif route == "send":
                self._handle_send(body)
            elif route == "init":
                self._handle_init(body)
            elif route == "userInfo":
                self._handle_user_info(body)
            else:
                env = envelope(body)
                logger.debug(f"[CONV] cmd={env['name']} code={env['code']}")
        except Exception as e:
            self.errors.append((url, str(e)))

    def _handle_login(self, body):
        r = scrape_ssr(body)
        if r["verdict"] == "logged_in" or self.login["verdict"] == "unknown":
            self.login = r
        label = {"logged_in": "✅ 已登录", "logged_out": "⛔ 未登录", "unknown": "❓ 未知"}
        logger.info(f"[LOGIN] {label.get(self.login['verdict'])}  user_id={self.login['user_id'] or '-'}"
                    + (f"  nickname={self.login['nickname']}" if self.login["nickname"] else ""))

    def _handle_send(self, body):
        r = decode_send_resp(body)
        r["at"] = time.time()
        self.sends.append(r)
        _msg = (f"[SEND] {'✅ 成功' if r['ok'] else '❌ 失败'}  code={r['code']}  "
                f'status="{r["status"]}"'
                + (f"  message_id={r['message_id']}" if r["message_id"] else ""))
        (logger.info if r["ok"] else logger.warning)(_msg)

    def _handle_init(self, body):
        r = decode_init_resp(body)
        self.list_rounds.append(r)
        self.list_has_more = r["has_more"]
        if r["complete"]:
            self.list_complete = True
        logger.debug(f"[LIST] 第 {len(self.list_rounds)} 轮  messages={r['msg_count']}  "
                     f"has_more={r['has_more']}  → "
                     + ("✅ 列表已加载完整" if r["complete"] else "⏳ 还有下一页"))

    def _handle_user_info(self, body):
        peers = decode_user_info(body)
        added = 0
        for p in peers:
            key = p["sec_uid"] or p["uid"]
            if not key:
                continue
            if key not in self.peers:
                self.peer_order.append(key)
                added += 1
            self.peers[key] = p
        if added:
            logger.debug(f"[PEER] 新增 {added} 人（累计 {len(self.peer_order)}）")
            for k in self.peer_order[-added:]:
                p = self.peers[k]
                logger.debug(
                    f"        {p['nickname'] or '(无昵称)'}"
                    + (f"  备注={p['remark']}" if p["has_remark"] else "  (无备注)")
                    + f"  uid={p['uid']}  抖音号={p['unique_id'] or '-'}")

    # ---- 对外 ----
    def by_sec_uid(self):
        return {p["sec_uid"]: p for p in self.peers.values() if p["sec_uid"]}

    def by_uid(self):
        return {p["uid"]: p for p in self.peers.values() if p["uid"]}

    def snapshot(self):
        peers = [self.peers[k] for k in self.peer_order]
        return {
            "login": dict(self.login),
            "list": {"complete": self.list_complete, "has_more": self.list_has_more,
                     "rounds": len(self.list_rounds)},
            "peers": peers,
            "peer_count": len(peers),
            "with_remark": [p for p in peers if p["has_remark"]],
            "sends": self.sends,
            "hits": dict(self.hits),
        }

    def detach(self):
        try:
            self.page.remove_listener("response", self._on_response)
        except Exception:
            pass


def attach(page):
    """挂监听。必须在 page.goto 之前调用，否则首屏的响应会漏掉。"""
    return ImMonitor(page)


# ===========================================================================
# 七、登录态判定（任务一的输入）
# ===========================================================================

LOGIN_LOGS = {
    "LOGGED_IN": "[LOGIN] ✅ 已登录",
    "NOT_LOGGED_IN": "[LOGIN] ❌ 未登录（没有 sessionid）→ 需要扫码登录",
    "EXPIRED": "[LOGIN] ⚠️ 登录已失效（有 sessionid 但服务端不认）→ 需要重新登录",
    "UNKNOWN": "[LOGIN] ❓ 登录态未知（SSR 与 DOM 都没给出结论）",
}


def check_login(page, mon):
    """
    登录态判定：SSR/监听（权威） → DOM 兜底 → cookie 佐证。
    返回 {state, user_id, nickname, sec_uid, log}；结论行由调用方按级别记日志。
    """
    state, user_id, nickname, sec_uid = "UNKNOWN", None, None, None

    # ① 监听拿到的 SSR（最准）
    li = mon.login if mon else {}
    if li.get("verdict") == "logged_in" and li.get("user_id"):
        state, user_id, nickname, sec_uid = ("LOGGED_IN", li["user_id"],
                                             li.get("nickname"), li.get("sec_uid"))

    # ② 自己再扫一遍页面 HTML（SSR 脚本节点还在 DOM 里）
    if state == "UNKNOWN":
        try:
            html = page.content()
        except Exception:
            html = ""
        s = scrape_ssr(html)
        if s["verdict"] == "logged_in" and s["user_id"]:
            state, user_id, nickname, sec_uid = ("LOGGED_IN", s["user_id"],
                                                 s["nickname"], s["sec_uid"])
        elif s["verdict"] == "logged_out":
            state = "NOT_LOGGED_IN"

    # ③ DOM 兜底
    if state == "UNKNOWN":
        try:
            dom = page.evaluate(JS_LOGIN_DOM)
        except Exception:
            dom = {}
        if dom.get("loginVisible"):
            state = "NOT_LOGGED_IN"
        elif dom.get("avatarCard") or dom.get("hasChatRoot"):
            state = "LOGGED_IN"      # 有聊天根节点，但没拿到 uid
            nickname = nickname or None

    # ④ cookie 佐证：有 sessionid 但判未登录 → 判为"已失效"而不是"未登录"
    if state == "NOT_LOGGED_IN":
        try:
            names = [c.get("name", "") for c in page.context.cookies()]
        except Exception:
            names = []
        if any(n.startswith("sessionid") for n in names):
            state = "EXPIRED"

    line = LOGIN_LOGS.get(state, LOGIN_LOGS["UNKNOWN"])
    if state in ("LOGGED_IN", "EXPIRED") and user_id:
        line += f"  user_id={user_id}"
    if nickname:
        line += f"  nickname={nickname}"

    return {"state": state, "user_id": user_id, "nickname": nickname,
            "sec_uid": sec_uid, "log": line}


# ---------------------------------------------------------------------------
# 就绪状态与结果
# ---------------------------------------------------------------------------

STATUS_READY = "READY"
STATUS_LOGGED_OUT = "LOGGED_OUT"
STATUS_EXPIRED = "EXPIRED"
STATUS_LOGIN_LOST = "LOGIN_LOST"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_ERROR = "ERROR"

READY_STATUSES = (STATUS_READY, STATUS_LOGGED_OUT, STATUS_EXPIRED,
                  STATUS_LOGIN_LOST, STATUS_TIMEOUT, STATUS_ERROR)


class DouyinIM:
    """会话列表扫描器。

    用法::

        im = DouyinIM(page)                       # 挂钩子 + goto + 跑门禁
        res = im.wait_ready()                     # 阻塞等门禁结论
        if res.status != "READY":
            return
        for hit in im.iter_find_and_select(["甲同学", "乙同学"]):
            im.type_and_send(hit, "在吗")          # 此刻会话已选中
        print(im.last_scan)
    """

    def __init__(self, page, url=CHAT_URL, timeout=90.0, max_steps=200,
                 settle_ms=800, ready_timeout=30.0, wait_until="domcontentloaded",
                 auto_goto=True):
        self.page = page
        self.url = url
        self.timeout = timeout              # 扫描总预算（秒）
        self.max_steps = max_steps          # 滚动步数硬上限
        self.settle_ms = settle_ms          # 资料静默窗（毫秒）
        self.ready_timeout = ready_timeout  # 门禁等待上限（秒）

        self._state = None                  # 就绪结果缓存
        self._callbacks = []
        self._login_lost = []
        self._select_failed = []
        self._detached = False
        self.last_scan = None
        self._scan_cache = {}               # conv_id -> hit（跨调用复用）
        self._walk_stat = {"stopped": "not-run", "steps": 0, "seen": {}}

        # ① 先挂钩子，再导航。顺序反了会漏首屏响应。
        self.mon = ImMonitor(page)
        self._watch_login_lost()

        if auto_goto:
            self._goto(wait_until)

        # ② 门禁在构造里就跑起来，on_ready 注册晚了也能立刻拿到结论
        self._state = self._run_preflight()

    # ---------------------------------------------------------------- 内部

    def _goto(self, wait_until):
        try:
            self.page.goto(self.url, wait_until=wait_until)
        except Exception as e:
            logger.warning(f"[IM] ⚠️ 导航异常（继续尝试判定）：{e}")
        # 留一点时间给首屏脚本与弹窗
        try:
            self.page.wait_for_timeout(1200)
        except Exception:
            pass

    def _watch_login_lost(self):
        """运行期掉登录监控：IM 响应 status_code=8 是官方「掉登录」码。

        挂在 response 上而不是轮询，是为了「任意时刻」都能捕获。
        """
        def on_resp(resp):
            try:
                url = resp.url
            except Exception:
                return
            if "imapi.douyin.com" not in url and "/im/" not in url:
                return
            try:
                body = resp.body()
            except Exception:
                return
            if not body or len(body) > 4096:
                return
            code = _envelope_code(body)
            if code == 8:
                self._emit_login_lost(code)
        try:
            self.page.on("response", on_resp)
        except Exception:
            pass
        self._on_resp_login_lost = on_resp

    def _emit_login_lost(self, code):
        logger.error(f"[LOGIN] ⛔ 运行期检测到掉登录（status_code={code}）")
        for cb in list(self._login_lost):
            try:
                cb({"code": code, "at": time.time()})
            except Exception:
                traceback.print_exc()

    def _run_preflight(self):
        """两道门禁：登录态 → 列表就绪。"""
        logger.info("─" * 64)
        logger.info("抖音 IM · 操作前检查")
        logger.info("─" * 64)

        lg = check_login(self.page, self.mon)
        state = lg["state"]
        (logger.info if state in ("LOGGED_IN",) else logger.warning)(lg["log"])
        if state in ("NOT_LOGGED_IN",):
            return self._finish(STATUS_LOGGED_OUT, lg)
        if state in ("EXPIRED",):
            return self._finish(STATUS_EXPIRED, lg)

        ls = self._wait_list_ready(lg)
        if not ls["ready"]:
            return self._finish(STATUS_TIMEOUT, lg, ls)

        return self._finish(STATUS_READY, lg, ls)

    def _wait_list_ready(self, lg):
        """列表就绪：DOM 连续两次采样稳定即可。

        注意与「列表全量」的区别 —— 首屏只渲染一页（约 20-25 条），
        后续靠滚动补。这里只回答「能不能开始滚」。
        """
        t0 = time.time()
        last = None
        result = {"ready": False, "via": None, "count": 0, "net_complete": False}
        while time.time() - t0 < self.ready_timeout:
            try:
                dom = self.page.evaluate(JS_LIST_READY) or {}
            except Exception:
                dom = {}
            if dom.get("ready"):
                now = time.time()
                if last and last["count"] == dom["count"] and now - last["at"] >= 1.2:
                    result = {"ready": True, "via": "dom-stable",
                              "count": dom["count"],
                              "net_complete": bool(self.mon.list_complete)}
                    break
                if not last or last["count"] != dom["count"]:
                    last = {"count": dom["count"], "at": now}
            try:
                self.page.wait_for_timeout(300)
            except Exception:
                time.sleep(0.3)

        if result["ready"]:
            logger.info(f"[LIST] ✅ 会话列表就绪（{result['via']}）"
                        f"  当前渲染 {result['count']} 个会话"
                        f"  has_more={self.mon.list_has_more}")
        else:
            logger.warning(f"[LIST] ⚠️ {self.ready_timeout:.0f}s 内未判定列表就绪")
        return result

    def _finish(self, status, lg, ls=None):
        self._state = {
            "status": status,
            "user_id": lg.get("user_id"),
            "nickname": lg.get("nickname"),
            "sec_uid": lg.get("sec_uid"),
            "login_state": lg.get("state"),
            "list": ls or {"ready": False, "count": 0},
            "log": lg.get("log"),
        }
        tag = {"READY": "✅ 就绪", "LOGGED_OUT": "⛔ 未登录",
               "EXPIRED": "⚠️ 登录已失效", "LOGIN_LOST": "⛔ 掉登录",
               "TIMEOUT": "⚠️ 超时", "ERROR": "❌ 出错"}.get(status, status)
        (logger.info if status == STATUS_READY else logger.error)(
            f"[IM] {tag}  user_id={self._state['user_id'] or '-'}"
            + (f"  nickname={self._state['nickname']}" if self._state["nickname"] else ""))
        for cb in list(self._callbacks):
            try:
                cb(dict(self._state))
            except Exception:
                traceback.print_exc()
        return self._state

    # ---------------------------------------------------------- 对外：就绪

    def on_ready(self, cb):
        """注册就绪回调。若门禁已经出结果，立刻回调一次（不重复投递）。"""
        self._callbacks.append(cb)
        if self._state is not None:
            try:
                cb(dict(self._state))
            except Exception:
                traceback.print_exc()
        return self

    def on_login_lost(self, cb):
        """注册运行期掉登录回调。"""
        self._login_lost.append(cb)
        return self

    def wait_ready(self):
        """返回就绪结果（构造里已算完，这里只是取）。"""
        return dict(self._state or {})

    @property
    def ready(self):
        return bool(self._state and self._state.get("status") == STATUS_READY)

    @property
    def self_uid(self):
        return (self._state or {}).get("user_id")

    # ---------------------------------------------------- 对外：滚动找人

    def iter_find_and_select(self, targets):
        """从头滚到尾，逐个找出目标并选中。

        yield 出来的那一刻，该会话**已被选中且 conv_id 已校验**。
        调用方拿到就能直接输入发送，不需要再次定位。

        targets：原始关键词列表（备注 / 昵称 / 抖音号 / uid 皆可，内部会归一化）
        """
        targets = [t for t in (targets or []) if t]
        pending = {_norm(t): t for t in targets}
        found = {}

        logger.info("─" * 64)
        logger.info(f"抖音 IM · 滚动查找 {len(pending)} 个目标")
        logger.info("─" * 64)

        self._select_failed = []

        for new_items in self._walk():
            # 对「已见过但还没归并资料」的项补齐，再统一匹配
            for item in new_items:
                self._join(item)

            # 匹配：备注 > 昵称 > 抖音号 > uid > 标题 > 子串
            for item in new_items:
                key, how = self._match(item, pending)
                if not key:
                    continue
                item["matched_by"] = how
                pending.pop(key, None)
                found[key] = item
                logger.info(f"[SCAN] ✅ 命中 {item['display']}"
                            f"（依据={how}）conv_id={item['conv_id']}")

                ok = self._select_and_verify(item)
                if not ok:
                    logger.warning(f"[SCAN] ⚠️ {item['display']} 选中失败，继续扫描")
                    self._select_failed.append(item)
                    continue

                item["reselect"] = self._make_reselect(item)
                yield item

                if not pending:
                    self._walk_stat["stopped"] = "all-found"
                    logger.info(f"[SCAN] 🎯 {len(found)} 个目标全部找到，提前停止")
                    break
            if self._walk_stat["stopped"] == "all-found":
                break

        self._finish_scan(found, list(pending.values()))

    # ------------------------------------------------------------ 滚动骨架

    def _walk(self):
        """滚动会话列表，逐轮产出「本轮新出现」的会话项（已按 conv_id 去重）。

        滚动的全部复杂度只在这一处：回到顶部、超时、读窗失败重试、到底判定、
        步长守卫、虚拟窗口换页等待、到底后等防抖再比高度。
        虚拟列表下同一条会被重叠窗口反复读到，这里只在**首次出现**时产出一次。

        调用方想提前停就 break；结束统计一律写进 self._walk_stat。
        """
        probe = self._scroll_probe()
        if not probe.get("found"):
            logger.error("[SCAN] ❌ 找不到会话列表容器")
            self._walk_stat = {"stopped": "no-container", "steps": 0,
                               "seen": dict(self._scan_cache)}
            return

        self._scroll_to(0)                              # 回到顶部，保证从头扫
        self.page.wait_for_timeout(400)

        seen = dict(self._scan_cache)
        window = []
        steps = 0
        empty_rounds = 0
        failed_windows = 0
        stopped = "exhausted"
        finished = False        # while 自然跑完才算扫全；被外部 break 不算
        t0 = time.time()
        try:
            while steps < self.max_steps:
                if time.time() - t0 > self.timeout:
                    stopped = "timeout"
                    logger.warning(f"[SCAN] ⏱ 扫描超时（{self.timeout:.0f}s）")
                    break

                window = self._read_window()
                if window is None:
                    failed_windows += 1
                    logger.warning(f"[SCAN] ⚠️ 读取窗口失败（{failed_windows}/5）")
                    if failed_windows >= 5:
                        stopped = "read-error"
                        break
                    self.page.wait_for_timeout(500)
                    continue
                failed_windows = 0

                new_items = []
                for item in window:
                    cid = item.get("conv_id")
                    if not cid or cid in seen:
                        continue
                    seen[cid] = item                    # 按 conv_id 去重
                    new_items.append(item)
                if new_items:
                    self._settle()                      # 等 /im/user/info 落定

                yield new_items

                # ---- 推进 ----
                probe = self._scroll_probe()
                if not probe.get("found"):
                    stopped = "container-lost"
                    break

                if probe.get("atBottom"):
                    self.page.wait_for_timeout(1000)    # 跨过前端 1000ms 防抖
                    after = self._scroll_probe()
                    if after.get("scrollHeight", 0) <= probe.get("scrollHeight", 0):
                        logger.info("[SCAN] 🏁 已到底且高度不再增长，扫描结束")
                        stopped = "reach-bottom"
                        break
                    logger.debug("[SCAN] ⬇ 到底后高度增长，继续加载")
                    empty_rounds = 0
                    continue

                step = max(120, int(probe["clientHeight"] * 0.4))
                nxt = min(probe["scrollTop"] + step,
                          max(0, probe["scrollHeight"] - probe["clientHeight"]))
                self._scroll_to(nxt)
                steps += 1
                self.page.wait_for_timeout(400)         # 等虚拟窗口换页

                moved = self._scroll_probe().get("scrollTop")
                if moved == probe.get("scrollTop"):
                    empty_rounds += 1
                    logger.debug(
                        f"[SCAN] scrollTop 未变化（{moved}），已到底计数 {empty_rounds}/3")
                    if empty_rounds >= 3:
                        stopped = "no-move"
                        break
                else:
                    empty_rounds = 0
                    logger.debug(f"[SCAN] 步 {steps}：scrollTop {probe['scrollTop']} → {moved}"
                                 f"（窗口 {len(window)} 项，累计 {len(seen)}）")
            finished = True     # 只有 while 条件跑干才会走到这，break 不会
        finally:
            # 「stopped 还是初始值」+「while 没跑完」= 调用方提前 break，
            # 不能报 scanned_all —— 那会让 missing 被误读成"确实不存在"。
            if not finished and stopped == "exhausted":
                stopped = "caller-break"
            self._scan_cache = seen
            self._walk_stat = {"stopped": stopped, "steps": steps, "seen": seen}

    def _finish_scan(self, found, missing):
        """统一组装 last_scan（两种扫描共用）。"""
        st = self._walk_stat
        seen = st["seen"]
        stopped = st["stopped"]
        # all-found 是调用方提前 break 时写进去的，同样算「扫全了」
        scanned_all = stopped in ("reach-bottom", "all-found", "no-move", "exhausted")
        self.last_scan = {
            "found": found,
            "missing": missing,
            "select_failed": [c.get("display") for c in self._select_failed],
            "scanned_all": scanned_all,
            "steps": st["steps"],
            "stopped": stopped,
            "visited": len(seen),
            "note": ("未覆盖折叠组/陌生人组" if scanned_all
                     else "扫描未完成，missing 不代表不存在"),
        }
        logger.info(f"[SCAN] 结束：stopped={stopped} 步数={st['steps']} 访问={len(seen)} "
                    f"找到={len(found)} 未找到={len(missing)} scanned_all={scanned_all}")

    def iter_conversations(self):
        """从头滚到尾，逐个产出**全部**会话（任务二：采集会话）。

        yield 的项已 `_join` 过监听资料，字段与 `iter_find_and_select` 的命中项一致：
        display / remark / nickname / douyin_id / uid / sec_uid / conv_id /
        is_group / unread / is_muted / data_index。

        按 conv_id 去重，虚拟列表的重叠窗口不会重复产出。
        结束后查 `last_scan`：`scanned_all=False` 表示没扫完，结果只是「已扫到的」。
        """
        self._select_failed = []
        logger.info("─" * 64)
        logger.info("抖音 IM · 枚举全部会话")
        logger.info("─" * 64)

        for new_items in self._walk():
            for item in new_items:
                self._join(item)
                yield item

        self._finish_scan({}, [])

    def _read_window(self):
        try:
            rows = self.page.evaluate(JS_COLLECT) or []
        except Exception:
            return None
        out = []
        for r in rows:
            out.append({
                "conv_id": r.get("convId"),
                "title": r.get("title"),
                "nickname": None,
                "remark": None,
                "douyin_id": "",
                "uid": r.get("uid"),
                "sec_uid": r.get("secUid"),
                "short_id": r.get("shortId"),
                "is_group": _is_group(r.get("convId"), r.get("type"),
                                     r.get("participantCount")),
                "unread": r.get("unread"),
                "is_muted": r.get("isMuted"),
                "is_current": r.get("isCurrent"),
                "data_index": r.get("dataIndex"),
                "dom_index": r.get("index"),
                "from_fiber": r.get("fromFiber"),
                "display": r.get("title"),
                "matched_by": None,
            })
            # 单聊缺 uid 时从 conv_id 拆
            if out[-1]["uid"] is None and out[-1]["conv_id"]:
                out[-1]["uid"] = self._peer_uid_of(out[-1]["conv_id"])
        return out

    def _peer_uid_of(self, conv_id):
        """单聊 conv_id = 0:1:<uidA>:<uidB>，取不是自己的那个。"""
        me = self.self_uid
        if not conv_id or not me:
            return None
        return peer_uid_of(conv_id, me)

    def _join(self, item):
        """把监听拿到的 /im/user/info 资料合并进会话项（按 sec_uid → uid）。"""
        peer = None
        if item.get("sec_uid"):
            peer = self.mon.by_sec_uid().get(item["sec_uid"])
        if peer is None and item.get("uid"):
            peer = self.mon.by_uid().get(item["uid"])
        if peer:
            item["nickname"] = peer.get("nickname") or None
            item["remark"] = peer.get("remark") or None
            item["douyin_id"] = peer.get("unique_id") or ""
            item["sec_uid"] = item.get("sec_uid") or peer.get("sec_uid")
            item["uid"] = item.get("uid") or peer.get("uid")
        item["display"] = item.get("remark") or item.get("nickname") or item.get("title")
        return item

    def _settle(self):
        """等资料静默窗：最后一次 /im/user/info 之后 settle_ms 内无新增，视为落定。"""
        if not self.settle_ms:
            return
        deadline = time.time() + self.settle_ms / 1000.0
        last_count = len(self.mon.peer_order)
        quiet_since = time.time()
        while time.time() < deadline:
            self.page.wait_for_timeout(120)
            now_count = len(self.mon.peer_order)
            if now_count != last_count:
                last_count = now_count
                quiet_since = time.time()
            if (time.time() - quiet_since) * 1000 >= self.settle_ms:
                return

    def _match(self, item, pending):
        """匹配目标。优先级：备注 > 昵称 > 抖音号 > uid/sec_uid > 标题 > 子串。"""
        order = ("remark", "nickname", "douyin_id", "uid", "sec_uid", "title")
        for field in order:
            val = item.get(field)
            if not val:
                continue
            n = _norm(val)
            if n and n in pending:
                return n, field
        # 子串兜底：目标出现在显示名里
        joined = " ".join(_norm(item.get(f) or "") for f in
                          ("display", "title", "nickname", "remark"))
        for n, raw in pending.items():
            if n and n in joined:
                return n, "fuzzy"
        return None, None

    def _scroll_probe(self):
        try:
            return self.page.evaluate(JS_SCROLL_PROBE) or {"found": False}
        except Exception:
            return {"found": False}

    def _scroll_to(self, top):
        try:
            return self.page.evaluate(JS_SCROLL_TO, top)
        except Exception:
            return False

    # ---------------------------------------------------- 对外：选中/输入

    def _select_and_verify(self, item, attempts=3):
        """选中并校验 conv_id。虚拟化下 nth 会漂，所以每轮都重新定位。"""
        want = item.get("conv_id")
        for i in range(attempts):
            idx = self._dom_index_of(want)
            if idx is None:
                logger.debug(f"[SEL] 第 {i+1} 次：目标不在当前窗口，先滚回来")
                self._scroll_to(item.get("data_index", 0) * ROW_HEIGHT)
                self.page.wait_for_timeout(400)
                idx = self._dom_index_of(want)
                if idx is None:
                    continue
            try:
                self._mouse_select(idx)
            except Exception as e:
                logger.warning(f"[SEL] 第 {i+1} 次点击异常：{e}")
                continue
            self.page.wait_for_timeout(600)

            cur = self._current_conv()
            if not cur:
                continue
            if want and cur.get("convId") and cur["convId"] != want:
                logger.warning(f"[SEL] 第 {i+1} 次选中了 {cur.get('title')}（conv_id 不符）")
                continue
            logger.debug(f"[SEL] ✅ 已选中 {cur.get('title')}  conv_id={cur.get('convId')}")
            return True
        return False

    def _dom_index_of(self, conv_id):
        """在**当前窗口内**查 conv_id 对应的 DOM 下标。

        关键：查询与点击必须紧邻，中间不能被别的 evaluate 打断，
        否则虚拟列表重排后 nth 会指到别人身上。
        """
        try:
            rows = self.page.evaluate(JS_COLLECT) or []
        except Exception:
            return None
        for r in rows:
            if r.get("convId") == conv_id:
                return r.get("index")
        return None

    def _mouse_select(self, index):
        """真实鼠标选中。选中逻辑挂在 onMouseDown 上，click 也能触发，
        但 cloakbrowser 已开 humanize，这里走原生鼠标保证时序可控。"""
        handle = self.page.evaluate_handle(
            "(i) => document.querySelectorAll('[data-e2e=\"conversation-item\"]')[i]", index)
        el = handle.as_element()
        if el is None:
            raise RuntimeError(f"下标 #{index} 上没有会话元素")
        el.scroll_into_view_if_needed()
        box = el.bounding_box()
        if not box:
            raise RuntimeError(f"下标 #{index} 的元素不可见")
        x = box["x"] + box["width"] / 2
        y = box["y"] + min(box["height"] / 2, 26)
        self.page.mouse.move(x, y)
        self.page.mouse.down()
        self.page.mouse.up()
        return True

    def _current_conv(self):
        try:
            return self.page.evaluate(JS_CURRENT_CONV)
        except Exception:
            return None

    def _make_reselect(self, item):
        """给外部一个「重新选中」的抓手：发送失败重试时用。"""
        def reselect():
            return self._select_and_verify(item)
        return reselect

    # ---------------------------------------------------- 对外：输入发送

    def type_and_send(self, hit, text, wait_receipt=True, timeout=20.0):
        """给「已选中」的会话输入并发送。

        输入走真实键盘事件（Draft.js 依赖 beforeinput/keydown 序列更新 EditorState，
        直接改 DOM 或只派发 input 事件都不可靠）。拟人节奏由 cloakbrowser humanize 负责，
        这里不重复加延迟。

        返回 {ok, via, message_id, code, status, conv_id, display, reason}
        """
        if not hit:
            raise ValueError("hit is None")
        if not text:
            raise ValueError("text is empty")

        # ① 草稿残留自查：上一条没清干净会导致两条消息串发
        try:
            empty = self.page.evaluate(JS_EDITOR_EMPTY)
        except Exception:
            empty = None
        if empty is False:
            logger.debug("[SEND] ⚠️ 输入框已有内容（草稿残留），先清空")
            try:
                self.page.evaluate(JS_EDITOR_CLEAR)
                self.page.wait_for_timeout(200)
            except Exception:
                pass

        # ② 输入。用 contenteditable 本体，humanize 的「可编辑」检查能过
        editor = self._editor()
        if editor is None:
            raise RuntimeError("找不到聊天输入框")
        editor.click()
        # 两种换行都认（口径见 split_message_lines 的文档）
        lines = split_message_lines(text)
        if text != "\n".join(lines):
            logger.debug(f"[SEND] 换行归一：{text!r} → {lines!r}")
        for i, line in enumerate(lines):
            if line:
                self.page.keyboard.type(line)
            if i != len(lines) - 1:
                self.page.keyboard.press("Shift+Enter")

        sends_before = len(self.mon.sends)
        msg_before = self._msg_state()

        # ③ 发送：按钮优先（有内容时变红可点），退化到回车
        how = self._click_send()
        logger.debug(f"[SEND] 发送方式={how}  conv_id={hit.get('conv_id')}")

        if not wait_receipt:
            return {"ok": True, "via": how, "conv_id": hit.get("conv_id"),
                    "display": hit.get("display")}

        rc = self._wait_receipt(sends_before, msg_before, text, timeout)
        if rc["http"]:
            h = rc["http"]
            (logger.info if h["ok"] else logger.warning)(
                f"[SEND] {'✅' if h['ok'] else '❌'} HTTP 回执 code={h['code']} "
                f'status="{h["status"]}" message_id={h["message_id"] or "-"}')
        if rc["dom"]:
            logger.debug(f"[SEND] ✅ DOM 确认：消息数 {msg_before.get('count', '?')} "
                         f"→ {rc['dom']['count']}，最后一条是自己发的")
        if not rc["ok"]:
            logger.warning(f"[SEND] ⚠️ {timeout:.0f}s 内没拿到回执（可能走 WS 通道，或发送被拦）")
        return {
            "ok": rc["ok"],
            "via": ("http+dom" if rc["http"] and rc["dom"] else
                    "http" if rc["http"] else "dom" if rc["dom"] else None),
            "message_id": (rc["http"] or {}).get("message_id"),
            "code": (rc["http"] or {}).get("code"),
            "status": (rc["http"] or {}).get("status"),
            "conv_id": hit.get("conv_id"),
            "display": hit.get("display"),
        }

    def _editor(self):
        for sel in ('[data-e2e="msg-input"] .public-DraftEditor-content',
                    '.DraftEditor-root [contenteditable="true"]',
                    '.messageEditorimChatEditorContainer'):
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    def _msg_state(self):
        try:
            return self.page.evaluate(JS_MSG_STATE) or {}
        except Exception:
            return {}

    def _click_send(self):
        try:
            btn = self.page.locator(SEL_SEND_BTN_READY).first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                return "button"
        except Exception:
            pass
        self.page.keyboard.press("Enter")
        return "enter"

    def _wait_receipt(self, sends_before, msg_before, text, timeout):
        """回执双确认：HTTP 监听（权威）+ DOM（WS 通道下补位）。"""
        t0 = time.time()
        http = dom = None
        while time.time() - t0 < timeout:
            if http is None:
                for rec in self.mon.sends[sends_before:]:
                    if rec.get("ok"):
                        http = rec
                        break
            if dom is None:
                st = self._msg_state()
                if (st.get("count", 0) > msg_before.get("count", 0)
                        and st.get("lastFromMe")):
                    head = (text or "")[:8]
                    if not head or (st.get("lastText") or "").startswith(head):
                        dom = st
            if http and dom:
                break
            if http and time.time() - t0 > 1.0:
                break
            if dom and time.time() - t0 > 2.0:
                break
            self.page.wait_for_timeout(150)
        return {"ok": bool(http or dom), "http": http, "dom": dom}

    # -------------------------------------------------------------- 其它

    def fold_groups(self):
        """折叠组 / 陌生人组是独立容器，主容器扫不到。这里列出它们的条目数，
        供调用方判断「找不到」是否可能因为目标被折叠。"""
        out = {}
        for name, sel in (("fold", SEL_LIST_FOLD), ("stranger", SEL_LIST_STRANGER)):
            try:
                out[name] = self.page.evaluate(
                    "(s) => document.querySelectorAll(s).length", sel)
            except Exception:
                out[name] = None
        return out

    def detach(self):
        """摘钩子。"""
        if self._detached:
            return
        try:
            self.mon.detach()
        except Exception:
            pass
        try:
            self.page.remove_listener("response", self._on_resp_login_lost)
        except Exception:
            pass
        self._detached = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.detach()
        return False
