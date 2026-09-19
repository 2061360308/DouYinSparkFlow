"""
core/msg_builder.py
解析消息模板构建具体发送的消息内容
"""

from utils.config import get_config
from utils.hitokoto import request_hitokoto
from datetime import date


def build_message_with_openai() -> str:
    """
    通过 OpenAI 接口生成续火花消息，内容丰富，不超过20字
    """
    from openai import OpenAI

    import os

    config = get_config()
    openai_config = config.get("openai", {})
    api_key = os.getenv("OPENAI_API_KEY", openai_config.get("api_key", ""))
    model = openai_config.get("model", "MiniMax-M2.7")

    if not api_key:
        return get_config().get("messageTemplate", "续火花")

    client = OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "你是一个擅长写续火花消息的助手。用户需要你生成一段不超过20字的续火花消息，内容要温馨、有趣、适合发给聊天对象。请直接输出消息内容，不要加引号或其他修饰。",
            },
            {"role": "user", "content": "生成一段续火花消息，直接输出内容不要思考过程"},
        ],
        extra_body={"reasoning_split": True},
    )

    print(response)

    return response.choices[0].message.content.strip()


def build_message() -> str:
    message = get_config().get("messageTemplate", "续火花")
    # 换行归一，两种 CRLF 都收成真 \n：
    #   ① 字面 \r\n（反斜杠+r+反斜杠+n 四字符）→ 字面 \n：手写 .env 可能是这样，
    #      不收的话下游会漏下一个裸露的 \r 打进草稿。
    #   ② 真 CRLF（U+000D U+000A）→ 真 \n：第三方内容（一言）可能带进来。
    # 两条规则作用在互不相交的字符集上（字面串里没有真的 CR/LF），顺序无所谓。
    # 只做这两步：不再额外 replace("\r","\n")，否则会把「字面 \r + 真换行」
    # 这类组合也吞掉，而那是作者的本意。
    message = message.replace("\\r\\n", "\\n").replace("\r\n", "\n")
    if "[API]" in message:
        api_content = request_hitokoto()
        message = message.replace("[API]", api_content)

    return message.strip()
