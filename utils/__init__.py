def norm(text):
    """统一好友昵称 / 备注 / 抖音号，避免空白差异导致匹配失败。"""
    if text is None:
        return ""
    return " ".join(str(text).split())
