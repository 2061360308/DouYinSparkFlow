import os
import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData, sanitize_cookies
from utils import norm
from core.msg_builder import build_message
from core.browser import get_browser
from playwright.sync_api import Response
import time

HOME_URL = "https://www.douyin.com/"
CHAT_URL = "https://www.douyin.com/chat"
LOGIN_URL_HINTS = ("login", "passport", "scan", "sso")
LOGIN_TEXT_HINTS = ("扫码登录", "手机号登录", "验证码登录", "登录后即可")

# 官网私信页（www.douyin.com/chat）会话列表 / 输入框
CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"

complates = {}

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
matchMode = config.get("matchMode", "nickname")
userIDDict = {}


def handle_response(response: Response):
    """监听官网私信用户信息，用于昵称 / 备注 / 抖音号匹配。"""
    global userIDDict
    if "aweme/v1/web/im/user/info" not in response.url:
        return
    try:
        json_data = response.json()
        for item in json_data.get("data", []):
            short_id = item.get("short_id")
            unique_id = item.get("unique_id")
            sec_uid = item.get("sec_uid", "")
            nickname = norm(item.get("nickname"))
            remark_name = norm(item.get("remark_name", nickname))
            userIDDict[remark_name] = [short_id, unique_id, sec_uid, nickname, remark_name]
            if nickname and nickname != remark_name:
                userIDDict[nickname] = [short_id, unique_id, sec_uid, nickname, remark_name]
    except Exception as e:
        tb = traceback.extract_tb(e.__traceback__)
        last = tb[-1]
        print(f"解析响应失败: {e}")
        print(f"文件: {last.filename}, 行号: {last.lineno}, 函数: {last.name}")


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                time.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise


def dump_page_debug(page, username, tag):
    os.makedirs("logs", exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() else "_" for ch in str(username))[:40] or "user"
    prefix = os.path.join("logs", f"{safe_name}_{tag}")
    try:
        page.screenshot(path=f"{prefix}.png", full_page=True)
    except Exception as e:
        logger.warning(f"账号 {username} 截图失败: {e}")
    try:
        with open(f"{prefix}.html", "w", encoding="utf-8") as handle:
            handle.write(page.content())
    except Exception as e:
        logger.warning(f"账号 {username} 保存 HTML 失败: {e}")
    logger.error(f"账号 {username} 页面调试信息 url={page.url} title={page.title()} 文件前缀={prefix}")


def page_looks_logged_out(page):
    url = (page.url or "").lower()
    if any(hint in url for hint in LOGIN_URL_HINTS):
        return True
    try:
        body = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return False
    has_login_text = any(hint in body for hint in LOGIN_TEXT_HINTS)
    has_header = page.locator("#douyin-header-menuCt").count() > 0
    return has_login_text and not has_header


def check_target_name(target_name, targets):
    target_name = norm(target_name)
    if target_name in userIDDict:
        matched = next((value for value in userIDDict[target_name] if value and value in targets), None)
        if matched is not None:
            return matched
    if target_name in targets:
        return target_name
    return None


def open_official_chat(page, username):
    """从官网顶栏「消息」进入私信页，失败则直接打开 /chat。"""
    retry_operation(
        "打开抖音官网",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url=HOME_URL,
    )
    if page_looks_logged_out(page):
        dump_page_debug(page, username, "homepage_logged_out")
        raise RuntimeError(
            f"账号 {username} Cookie 未生效，仍停留在登录页 {page.url}。"
            "请在已登录的 www.douyin.com 用 Cookie-Editor 重新导出 Cookie。"
        )

    clicked = False
    header = page.locator("#douyin-header-menuCt, #douyin-header")
    try:
        message_entry = header.get_by_text("消息", exact=True).first
        message_entry.wait_for(state="visible", timeout=15000)
        message_entry.click()
        clicked = True
        logger.info(f"账号 {username} 已点击官网顶栏「消息」")
    except Exception as e:
        logger.warning(f"账号 {username} 点击顶栏「消息」失败，改走 /chat: {e}")

    if not clicked or "/chat" not in (page.url or ""):
        retry_operation(
            "打开抖音官网私信页",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url=CHAT_URL,
        )

    time.sleep(5)
    try:
        page.locator(CONVERSATION_LIST_SELECTOR).first.wait_for(state="visible")
    except Exception:
        dump_page_debug(page, username, "chat_list_timeout")
        if page_looks_logged_out(page):
            raise RuntimeError(
                f"账号 {username} 未登录官网私信页（当前 {page.url}）。"
                "请在 www.douyin.com 重新导出 Cookie。"
            )
        raise


def scroll_and_select_user(page, username, targets):
    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")

    found_targets = set()
    remaining_targets = set(targets)
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10

    while True:
        target_elements = page.locator(CONVERSATION_ITEM_SELECTOR).all()
        prev_found_count = len(found_targets)

        for element in target_elements:
            try:
                target_name = element.locator(CONVERSATION_TITLE_SELECTOR).inner_text()
                if target_name in found_targets:
                    continue
                found_targets.add(target_name)
                logger.debug(f"账号 {username} 找到好友 {target_name}")

                target_symbol = check_target_name(target_name, targets)
                if not target_symbol:
                    continue

                element.click()
                yield target_symbol
                if target_symbol in remaining_targets:
                    remaining_targets.remove(target_symbol)
                if not remaining_targets:
                    logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                    return
                break
            except Exception:
                traceback.print_exc()
        else:
            new_found = len(found_targets) > prev_found_count
            empty_scroll_count = 0 if new_found else empty_scroll_count + 1

            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                logger.warning(
                    f"账号 {username} 连续 {MAX_EMPTY_SCROLLS} 次滚动未发现新好友，判定已到达底部"
                )
                if remaining_targets:
                    logger.warning(f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}")
                break

            scrollable_element = page.locator(CONVERSATION_LIST_SELECTOR).element_handle()
            if not scrollable_element:
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                break

            scroll_top_before = page.evaluate("(element) => element.scrollTop", scrollable_element)
            page.evaluate("(element) => element.scrollTop += 800", scrollable_element)
            time.sleep(0.3)
            scroll_top_after = page.evaluate("(element) => element.scrollTop", scrollable_element)

            if scroll_top_before == scroll_top_after:
                empty_scroll_count += 2
                logger.debug(
                    f"账号 {username} scrollTop 未变化 ({scroll_top_before})，可能已到底 (空滚动计数: {empty_scroll_count}/{MAX_EMPTY_SCROLLS})"
                )
            else:
                logger.debug(
                    f"账号 {username} 滚动好友列表以加载更多好友 (scrollTop: {scroll_top_before} -> {scroll_top_after})"
                )
            time.sleep(1.5)


def send_chat_message(page, username, target_name):
    page.wait_for_selector(CHAT_EDITOR_SELECTOR, timeout=config["browserTimeout"])
    chat_input = page.locator(CHAT_EDITOR_SELECTOR)
    message = build_message()
    lines = message.split("\\n")
    for index, line in enumerate(lines):
        chat_input.type(line)
        if index != len(lines) - 1:
            chat_input.press("Shift+Enter")
    logger.debug(f"账号 {username} 准备发送消息给好友 {target_name}：\n\t{message}")
    chat_input.press("Enter")
    logger.debug(f"账号 {username} 给好友 {target_name} 发送消息完成")
    time.sleep(2)


def do_user_task(browser, username, cookies, targets):
    context = browser.new_context(
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1440, "height": 900},
    )
    context.set_default_navigation_timeout(config["browserTimeout"])
    context.set_default_timeout(config["browserTimeout"])

    page = context.new_page()
    page.on("response", handle_response)

    cookies = sanitize_cookies(cookies)
    logger.info(f"账号 {username} 注入 {len(cookies)} 条 cookie")
    context.add_cookies(cookies)

    open_official_chat(page, username)

    logger.debug(f"账号 {username} 开始发送消息")
    for target_name in scroll_and_select_user(page, username, targets):
        logger.debug(f"账号 {username} 已选中好友 {target_name} 发送消息")
        send_chat_message(page, username, target_name)

    context.close()


def runTasks():
    playwright, browser = get_browser()
    try:
        logger.info("开始执行任务")
        logger.debug("当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}")

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            complates[user["unique_id"]] = []
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            do_user_task(browser, username, cookies, targets)
            logger.info(f"账号 {username} 任务完成")
    finally:
        browser.close()
        playwright.stop()
