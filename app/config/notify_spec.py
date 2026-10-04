"""消息通知方式的规格（类型 + 参数字段）。

一份定义两处用：
  - 前端：app/web/service.py 把它塞进 options.notify_types，页面据此动态渲染配置面板
  - 后端：app/config/models.py 用它对 NOTIFY 里的每条通知做校验

通知配置存进 .env 的 ``NOTIFY``，是一个 JSON 数组，每条形如::

    {"type": "bark", "enabled": true, "bark_url": "https://api.day.app/xxx"}

字段类型：
  text   普通文本输入
  bool   开关（存 true/false）
  select 下拉选择（options 里给 value/label）
"""

from __future__ import annotations

# 各通知方式的参数字段。key 与 core/notify.py 里读取的键一一对应。
NOTIFY_TYPES = [
    {
        "id": "server",
        "label": "Server 酱",
        "fields": [
            {"key": "sckey", "label": "SCKEY", "required": True, "placeholder": "SCT..."},
        ],
    },
    {
        "id": "server_turbo",
        "label": "Server 酱 Turbo / ³",
        "fields": [
            {
                "key": "sendkey",
                "label": "SendKey",
                "required": True,
                "placeholder": "sctp...t 或 SCT...",
            },
        ],
    },
    {
        "id": "coolpush",
        "label": "Cool Push",
        "fields": [
            {"key": "coolpushskey", "label": "SKey", "required": True},
            {"key": "coolpushqq", "label": "推送到 QQ", "type": "bool", "default": True},
            {"key": "coolpushwx", "label": "推送到微信", "type": "bool", "default": False},
            {"key": "coolpushemail", "label": "推送到邮箱", "type": "bool", "default": False},
        ],
    },
    {
        "id": "qmsg",
        "label": "Qmsg 酱",
        "fields": [
            {"key": "qmsg_key", "label": "Key", "required": True},
            {
                "key": "qmsg_type",
                "label": "发送类型",
                "type": "select",
                "default": "private",
                "options": [
                    {"value": "private", "label": "私聊"},
                    {"value": "group", "label": "群聊"},
                ],
            },
        ],
    },
    {
        "id": "telegram",
        "label": "Telegram",
        "fields": [
            {"key": "tg_bot_token", "label": "Bot Token", "required": True},
            {"key": "tg_user_id", "label": "Chat / User ID", "required": True},
            {"key": "tg_api_host", "label": "API Host（可选）", "placeholder": "自建反代地址"},
            {"key": "tg_proxy", "label": "代理（可选）", "placeholder": "http://127.0.0.1:7890"},
        ],
    },
    {
        "id": "feishu",
        "label": "飞书机器人",
        "fields": [
            {"key": "fskey", "label": "Webhook Key", "required": True},
        ],
    },
    {
        "id": "dingtalk",
        "label": "钉钉机器人",
        "fields": [
            {"key": "dingtalk_access_token", "label": "Access Token", "required": True},
            {"key": "dingtalk_secret", "label": "加签 Secret", "required": True},
        ],
    },
    {
        "id": "bark",
        "label": "Bark（iOS）",
        "fields": [
            {
                "key": "bark_url",
                "label": "Bark URL",
                "required": True,
                "placeholder": "https://api.day.app/你的key",
            },
        ],
    },
    {
        "id": "qywx_robot",
        "label": "企业微信群机器人",
        "fields": [
            {"key": "qywx_key", "label": "Webhook Key", "required": True},
        ],
    },
    {
        "id": "qywx_app",
        "label": "企业微信应用",
        "fields": [
            {"key": "qywx_corpid", "label": "CorpID", "required": True},
            {"key": "qywx_corpsecret", "label": "CorpSecret", "required": True},
            {"key": "qywx_agentid", "label": "AgentID", "required": True},
            {"key": "qywx_touser", "label": "接收人", "required": True, "placeholder": "@all 或用户ID"},
            {"key": "qywx_media_id", "label": "封面 media_id（可选）"},
            {"key": "qywx_origin", "label": "私有化地址（可选）", "placeholder": "https://qyapi.weixin.qq.com"},
        ],
    },
    {
        "id": "pushplus",
        "label": "PushPlus",
        "fields": [
            {"key": "pushplus_token", "label": "Token", "required": True},
            {"key": "pushplus_topic", "label": "群组 Topic（可选）"},
        ],
    },
    {
        "id": "gotify",
        "label": "Gotify",
        "fields": [
            {"key": "gotify_url", "label": "服务地址", "required": True, "placeholder": "https://gotify.example.com"},
            {"key": "gotify_token", "label": "Token", "required": True},
            {"key": "gotify_priority", "label": "优先级", "default": "3"},
        ],
    },
    {
        "id": "ntfy",
        "label": "Ntfy",
        "fields": [
            {"key": "ntfy_url", "label": "服务地址（可选）", "placeholder": "https://ntfy.sh"},
            {"key": "ntfy_topic", "label": "Topic", "required": True},
            {"key": "ntfy_priority", "label": "优先级", "default": "3"},
        ],
    },
]

_BY_ID = {item["id"]: item for item in NOTIFY_TYPES}


def spec_for(type_id: str) -> dict:
    """按类型 id 取规格；未知类型返回空 dict。"""
    return _BY_ID.get(str(type_id or "").strip(), {})


def label_for(type_id: str) -> str:
    return spec_for(type_id).get("label", str(type_id or ""))


def known_ids() -> set:
    return set(_BY_ID)


def default_params(type_id: str) -> dict:
    """按规格生成一条新通知的默认字段值。"""
    spec = spec_for(type_id)
    params: dict = {}
    for field in spec.get("fields", []):
        if field.get("type") == "bool":
            params[field["key"]] = bool(field.get("default", False))
        else:
            params[field["key"]] = field.get("default", "")
    return params


def missing_required(item: dict) -> list:
    """返回该条通知里缺失的必填字段标签列表。"""
    spec = spec_for((item or {}).get("type"))
    missing: list = []
    for field in spec.get("fields", []):
        if not field.get("required"):
            continue
        value = (item or {}).get(field["key"])
        if field.get("type") == "bool":
            continue
        if value is None or str(value).strip() == "":
            missing.append(field["label"])
    return missing
