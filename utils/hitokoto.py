import re

import requests

from utils.config import get_config

hitokotoApi = "https://v1.hitokoto.cn/"

allHitokotoTypes = {
    "动画": "a",
    "漫画": "b",
    "游戏": "c",
    "文学": "d",
    "原创": "e",
    "来自网络": "f",
    "其他": "g",
    "影视": "h",
    "诗词": "i",
    "哲学": "k",
    "抖机灵": "l",
}


def request_hitokoto():
    """请求一言 API 获取一句话"""
    config = get_config()
    
    api_url = hitokotoApi

    for t in allHitokotoTypes.keys():
        if t in config["hitokotoTypes"]:
            if "?" not in api_url:
                api_url += "?"
            if "c=" in api_url:
                api_url += f"&c={allHitokotoTypes[t]}"
            else:
                api_url += f"c={allHitokotoTypes[t]}"

    try:
        response = requests.get(api_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        theFrom = data.get("from")
        if theFrom is None or theFrom.strip() == "":
            theFrom = "未知来源"
        theFromWho = data.get("from_who")
        if theFromWho is None or theFromWho.strip() == "":
            theFromWho = "未知作者"
        # 一言正文/来源/作者都可能带换行，直接拼进模板会把行结构拆散
        # （模板靠 \n 分行发送）。这里统一拍平成单行：连续空白收成一个空格。
        # 用 .get 而不是下标：缺字段时原样抛 KeyError 会被下面的 except 吞掉，
        # 退化成"[error] 无法获取一言内容"，问题被掩盖。
        quote = re.sub(r"\s+", " ", str(data.get("hitokoto", ""))).strip()
        theFrom = re.sub(r"\s+", " ", theFrom).strip()
        theFromWho = re.sub(r"\s+", " ", theFromWho).strip()
        return f"{quote} —— {theFrom} ({theFromWho})"
    except Exception as e:
        return "[error] 无法获取一言内容"
