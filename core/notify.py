"""消息通知：一轮任务跑完后把结果推到用户配置的渠道。

配置来自 .env 的 ``NOTIFY``（JSON 数组，每条 {"type", "enabled", ...参数}），
字段定义见 app/config/notify_spec.py。这里只负责「怎么发」。

每个发送函数成功返回 None，失败抛异常；send_all 逐条捕获，互不影响。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import quote_plus

import requests

from app.config import notify_spec

TITLE = "抖音火花续期"
TIMEOUT = 15


def _post(url, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT)
    resp = requests.post(url, **kwargs)
    resp.raise_for_status()
    return resp


def _get(url, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT)
    resp = requests.get(url, **kwargs)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# 各通知方式
# ---------------------------------------------------------------------------
def _server(item, content):
    _post(
        f"https://sc.ftqq.com/{item['sckey']}.send",
        data={"text": TITLE, "desp": content},
    )


def _server_turbo(item, content):
    sendkey = item["sendkey"]
    if match := re.match(r"^sctp(\d+)t", sendkey):
        url = f"https://{match.group(1)}.push.ft07.com/send/{sendkey}.send"
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
    _post(url, data={"text": TITLE, "desp": content})


def _coolpush(item, content):
    skey = item["coolpushskey"]
    params = {"c": content, "t": TITLE}
    if item.get("coolpushqq", True):
        _post(f"https://push.xuthus.cc/send/{skey}", params=params)
    if item.get("coolpushwx"):
        _post(f"https://push.xuthus.cc/wx/{skey}", params=params)
    if item.get("coolpushemail"):
        _post(f"https://push.xuthus.cc/email/{skey}", params=params)


def _qmsg(item, content):
    key = item["qmsg_key"]
    path = "group" if item.get("qmsg_type") == "group" else "send"
    _get(f"https://qmsg.zendee.cn/{path}/{key}", params={"msg": content})


def _telegram(item, content):
    host = (item.get("tg_api_host") or "").strip()
    url = (
        f"https://{host}/bot{item['tg_bot_token']}/sendMessage"
        if host
        else f"https://api.telegram.org/bot{item['tg_bot_token']}/sendMessage"
    )
    proxy = (item.get("tg_proxy") or "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    _post(
        url,
        data={
            "chat_id": item["tg_user_id"],
            "text": content,
            "disable_web_page_preview": "true",
        },
        proxies=proxies,
    )


def _feishu(item, content):
    _post(
        f"https://open.feishu.cn/open-apis/bot/v2/hook/{item['fskey']}",
        json={"msg_type": "text", "content": {"text": content}},
    )


def _dingtalk(item, content):
    secret = item["dingtalk_secret"]
    token = item["dingtalk_access_token"]
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = quote_plus(base64.b64encode(hmac_code))
    _post(
        f"https://oapi.dingtalk.com/robot/send?access_token={token}"
        f"&timestamp={timestamp}&sign={sign}",
        headers={"Content-Type": "application/json", "Charset": "UTF-8"},
        data=json.dumps({"msgtype": "text", "text": {"content": content}}),
    )


def _bark(item, content):
    url = item["bark_url"].rstrip("/")
    _get(f"{url}/{quote_plus(content)}")


def _qywx_robot(item, content):
    _post(
        f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={item['qywx_key']}",
        data=json.dumps({"msgtype": "text", "text": {"content": content}}),
    )


def _qywx_app(item, content):
    base_url = (item.get("qywx_origin") or "https://qyapi.weixin.qq.com").rstrip("/")
    token = (
        _get(
            f"{base_url}/cgi-bin/gettoken",
            params={
                "corpid": item["qywx_corpid"],
                "corpsecret": item["qywx_corpsecret"],
            },
        )
        .json()
        .get("access_token")
    )
    if not token:
        raise RuntimeError("获取企业微信 access_token 失败")
    media_id = (item.get("qywx_media_id") or "").strip()
    if media_id:
        payload = {
            "touser": item["qywx_touser"],
            "agentid": int(item["qywx_agentid"]),
            "msgtype": "mpnews",
            "mpnews": {
                "articles": [
                    {
                        "title": TITLE,
                        "thumb_media_id": media_id,
                        "author": "DouYinSparkFlow",
                        "content_source_url": "https://github.com/2061360308/DouYinSparkFlow",
                        "content": content.replace("\n", "<br>"),
                        "digest": content,
                    }
                ]
            },
        }
    else:
        payload = {
            "touser": item["qywx_touser"],
            "agentid": int(item["qywx_agentid"]),
            "msgtype": "textcard",
            "textcard": {
                "title": TITLE,
                "description": content,
                "url": "https://github.com/2061360308/DouYinSparkFlow",
                "btntxt": "开源项目",
            },
        }
    _post(
        f"{base_url}/cgi-bin/message/send?access_token={token}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def _pushplus(item, content):
    data = {
        "token": item["pushplus_token"],
        "title": TITLE,
        "content": content.replace("\n", "<br>"),
        "template": "json",
    }
    topic = (item.get("pushplus_topic") or "").strip()
    if topic:
        data["topic"] = topic
    _post("https://www.pushplus.plus/send", data=json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _gotify(item, content):
    url = item["gotify_url"].rstrip("/")
    resp = _post(
        f"{url}/message",
        params={"token": item["gotify_token"]},
        data={
            "title": TITLE,
            "message": content,
            "priority": str(item.get("gotify_priority") or "3"),
        },
    ).json()
    if not resp.get("id"):
        raise RuntimeError(f"Gotify 返回异常：{resp}")


def _ntfy(item, content):
    base = (item.get("ntfy_url") or "https://ntfy.sh").rstrip("/")
    encoded_title = "=?utf-8?B?" + base64.b64encode(TITLE.encode("utf-8")).decode("utf-8") + "?="
    _post(
        f"{base}/{item['ntfy_topic']}",
        data=content.encode("utf-8"),
        headers={
            "Title": encoded_title,
            "Priority": str(item.get("ntfy_priority") or "3"),
        },
    )


HANDLERS = {
    "server": _server,
    "server_turbo": _server_turbo,
    "coolpush": _coolpush,
    "qmsg": _qmsg,
    "telegram": _telegram,
    "feishu": _feishu,
    "dingtalk": _dingtalk,
    "bark": _bark,
    "qywx_robot": _qywx_robot,
    "qywx_app": _qywx_app,
    "pushplus": _pushplus,
    "gotify": _gotify,
    "ntfy": _ntfy,
}


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def send_one(item, content: str) -> tuple:
    """发送单条通知。返回 (是否成功, 错误信息)。"""
    item = item or {}
    type_id = str(item.get("type") or "").strip()
    handler = HANDLERS.get(type_id)
    if handler is None:
        return False, f"未知的通知方式：{type_id!r}"
    if not item.get("enabled", True):
        return False, "已停用"
    try:
        handler(item, content)
        return True, ""
    except Exception as exc:  # noqa: BLE001 —— 一条失败不影响其它
        return False, f"{type(exc).__name__}: {exc}"


def send_all(notifications, content: str) -> list:
    """逐条发送，返回 [(标签, 是否成功, 错误信息)]。"""
    results: list = []
    for item in notifications or []:
        type_id = str((item or {}).get("type") or "").strip()
        label = notify_spec.label_for(type_id) or type_id or "未知"
        ok, message = send_one(item, content)
        results.append((label, ok, message))
    return results
