"""HAR 录制工具（**不是测试**）—— 打开抖音聊天页录一段流量供离线分析。

用法::

    python tools/record_har.py                      # 第一个账号，录 300 秒
    python tools/record_har.py --seconds 60         # 录 60 秒
    python tools/record_har.py --account 12345678901
    python tools/record_har.py --out har_logs/my.har

产出放在 har_logs/ 下，文件名 `<用户名>_<时间戳>.har`。

⚠️ 产出的 HAR **含登录凭据（cookie）与好友资料（昵称/备注/uid），绝不入库**。
   har_logs/ 已在 .gitignore 里；同目录还会生成一批 <sha1>.dat（二进制报文体），
   它们与 .har 是配套的，一起移动/一起删除。

⚠️ 为什么这里用 `record_har_content="attach"`：
   Playwright 录 HAR 时二进制响应的内容不写进 .har 的 content.text，
   而是落到同目录 <sha1>.dat 并由 content._file 引用。
   解析时必须带上同目录全部 .dat，否则 protobuf 全是空壳 —— 详见 har_logs 里的取证记录。
"""

import argparse
import os
import sys
import time
import traceback
from datetime import datetime

# 以 `python tools/record_har.py` 直接跑时，项目根目录不在 sys.path 里
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if os.path.exists(os.path.join(_ROOT, ".env")):
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_ROOT, ".env"))

from core.browser import get_browser           # noqa: E402
from utils.config import get_config, get_userData  # noqa: E402

HAR_LOG_DIR = os.path.join(_ROOT, "har_logs")
CHAT_URL = "https://www.douyin.com/chat"


def record(user, seconds, out_path=None):
    """录一个账号的 HAR，返回最终文件名。"""
    config = get_config()
    username = user.get("username", "未知用户")
    fingerprint = user.get("fingerprint", None)

    os.makedirs(HAR_LOG_DIR, exist_ok=True)
    har_path = out_path or os.path.join(
        HAR_LOG_DIR,
        f"{username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.har")

    print(f"开始录制 HAR → {har_path}")
    browser = get_browser(fingerprint)
    try:
        context = browser.new_context(
            record_har_path=har_path,
            record_har_content="attach",   # 二进制体落 <sha1>.dat（见模块 docstring）
        )
        context.set_default_navigation_timeout(config["browserActionTimeout"])
        context.set_default_timeout(config["browserActionTimeout"])
        try:
            page = context.new_page()
            context.add_cookies(user["cookies"])
            page.goto(CHAT_URL)
            print(f"页面已打开，录制 {seconds}s（期间请在页面上做要记录的操作）…")
            # 用 page.wait_for_timeout 而不是 time.sleep：
            # Playwright sync API 只在调用其接口时派发事件，纯 sleep 会漏事件。
            page.wait_for_timeout(int(seconds * 1000))
        finally:
            context.close()
    finally:
        browser.close()

    print(f"HAR 录制完成 → {har_path}")
    print("提示：同目录的 <sha1>.dat 是配套二进制体，分析时必须一起带上；"
          "两者都含敏感信息，不要提交。")
    return har_path


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="tools/record_har.py",
        description="录制抖音聊天页 HAR（产物含敏感信息，不入库）")
    ap.add_argument("--account", help="抖音号（.env 里 COOKIES_<抖音号> 的后缀）；默认取第一个")
    ap.add_argument("--seconds", type=float, default=300.0, help="录制时长（秒），默认 300")
    ap.add_argument("--out", help="输出路径；默认 har_logs/<用户名>_<时间戳>.har")
    args = ap.parse_args(argv)

    users = get_userData()
    if args.account:
        users = [u for u in users if str(u.get("unique_id")) == str(args.account)]
    if not users:
        print("❌ .env 里没读到可用账号（或 --account 不匹配）", file=sys.stderr)
        return 2

    try:
        record(users[0], args.seconds, args.out)
        return 0
    except Exception:
        print("【顶层捕获异常】", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
