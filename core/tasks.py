import os
import time
import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData, sanitize_cookies
from utils import norm
from core.msg_builder import build_message
from core.browser import get_browser
from playwright.sync_api import Response, TimeoutError as PlaywrightTimeoutError

HOME_URL = "https://www.douyin.com/"
CHAT_URL = "https://www.douyin.com/chat"
LOGIN_URL_HINTS = ("login", "passport", "scan", "sso")
LOGIN_TEXT_HINTS = ("扫码登录", "手机号登录", "验证码登录", "登录后即可")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/172.16.1.5 Safari/537.36"
)

# 官网私信页：class 来自 pcim CSS Module，data-e2e 相对更稳
CONVERSATION_ITEM_SELECTOR = (
    ".conversationConversationItemwrapper, [data-e2e='conversation-item']"
)
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
CHAT_EDITOR_SELECTOR = (
    ".messageEditorimChatEditorContainer, [data-e2e='msg-input']"
)
CHAT_INPUT_AREA_SELECTOR = ".messageEditorinputArea, [data-e2e='msg-input']"
SEND_API_HINT = "/v1/message/send"
HIDE_WEBDRIVER_SCRIPT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"

complates = {}

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
matchMode = config.get("matchMode", "nickname")
userIDDict = {}
send_results = []


def reset_runtime_state():
    userIDDict.clear()
    send_results.clear()


def remember_user(item):
    if not isinstance(item, dict):
        return
    short_id = norm(item.get("short_id"))
    unique_id = norm(item.get("unique_id"))
    sec_uid = norm(item.get("sec_uid"))
    nickname = norm(item.get("nickname"))
    remark_name = norm(item.get("remark_name") or nickname)
    record = [short_id, unique_id, sec_uid, nickname, remark_name]
    for key in (remark_name, nickname, short_id, unique_id, sec_uid):
        if key:
            userIDDict[key] = record


def handle_response(response: Response):
    """监听官网私信用户信息，用于昵称 / 备注 / 抖音号匹配。"""
    url = response.url or ""
    if SEND_API_HINT in url and response.request.method == "POST":
        send_results.append({"status": response.status, "ok": response.ok})
        logger.info(f"发送接口 {url.split('?', 1)[0]} 状态码 {response.status}")
        return
    if "aweme/v1/web/im/user/info" not in url:
        return
    try:
        json_data = response.json()
        items = json_data.get("data", [])
        if isinstance(items, dict):
            items = items.get("user_list") or items.get("users") or items.get("data") or []
        if not isinstance(items, list):
            return
        for item in items:
            remember_user(item)
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
    has_header = page.locator("#douyin-header-menuCt, #douyin-header").count() > 0
    return has_login_text and not has_header


def title_is_placeholder(name):
    text = norm(name)
    return (not text) or text.isdigit()


def identities_for_title(title):
    title = norm(title)
    record = userIDDict.get(title, [])
    values = [norm(item) for item in record if item]
    if title:
        values.append(title)
    return values


def check_target_name(target_name, targets):
    target_name = norm(target_name)
    record = userIDDict.get(target_name, [])
    short_id = norm(record[0]) if len(record) > 0 else ""
    unique_id = norm(record[1]) if len(record) > 1 else ""

    if matchMode == "short_id":
        for candidate in (short_id, unique_id, target_name):
            if candidate and candidate in targets:
                return candidate
        return None

    for candidate in identities_for_title(target_name):
        if candidate in targets:
            return candidate
    return None


def dismiss_interruptions(page):
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    for name in ("关闭", "以后再说", "暂不", "我知道了", "取消"):
        try:
            button = page.get_by_role("button", name=name).first
            if button.count() and button.is_visible():
                button.click(timeout=800)
        except Exception:
            continue


def wait_for_conversation_list(page, username):
    deadline = time.time() + max(20, config["browserTimeout"] / 1000)
    last_error = None
    while time.time() < deadline:
        dismiss_interruptions(page)
        try:
            if page.locator(CONVERSATION_ITEM_SELECTOR).count() > 0:
                return
            page.locator(CONVERSATION_LIST_SELECTOR).first.wait_for(state="visible", timeout=3000)
            if page.locator(CONVERSATION_ITEM_SELECTOR).count() > 0:
                return
        except Exception as e:
            last_error = e
        time.sleep(0.5)
    dump_page_debug(page, username, "chat_list_timeout")
    if page_looks_logged_out(page):
        raise RuntimeError(
            f"账号 {username} 未登录官网私信页（当前 {page.url}）。"
            "请在已登录的 www.douyin.com 用 Cookie-Editor 重新导出 Cookie。"
        )
    raise last_error or RuntimeError(f"账号 {username} 会话列表未出现")


def wait_for_user_profiles(page, timeout=12):
    # 行露出后才会打 user/info；先等两秒再轮询，避免标题还是 uid。
    time.sleep(2)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if userIDDict:
            return True
        time.sleep(0.4)
    try:
        page.locator(CONVERSATION_ITEM_SELECTOR).first.hover(timeout=2000)
    except Exception:
        pass
    time.sleep(1.5)
    return bool(userIDDict)


def open_official_chat(page, username):
    """先打开官网让签名 SDK 初始化，再进入 /chat。"""
    retry_operation(
        "打开抖音官网",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url=HOME_URL,
        wait_until="domcontentloaded",
    )
    if page_looks_logged_out(page):
        dump_page_debug(page, username, "homepage_logged_out")
        raise RuntimeError(
            f"账号 {username} Cookie 未生效，仍停留在登录页 {page.url}。"
            "请在已登录的 www.douyin.com 用 Cookie-Editor 重新导出 Cookie。"
        )

    retry_operation(
        "打开抖音官网私信页",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url=CHAT_URL,
        wait_until="domcontentloaded",
    )
    wait_for_conversation_list(page, username)
    wait_for_user_profiles(page)
    logger.info(f"账号 {username} 已进入官网私信页，已缓存 {len(userIDDict)} 条用户资料")


def read_item_title(element):
    title_node = element.locator(CONVERSATION_TITLE_SELECTOR)
    if title_node.count():
        return title_node.first.inner_text()
    return element.inner_text().split("\n", 1)[0]


def search_and_select_user(page, username, target):
    search = page.get_by_placeholder("搜索用户名字")
    if search.count() == 0:
        search = page.get_by_placeholder("搜索")
    if search.count() == 0:
        return False
    box = search.first
    box.click()
    box.fill("")
    box.fill(target)
    time.sleep(1.5)
    items = page.locator(CONVERSATION_ITEM_SELECTOR)
    for index in range(min(items.count(), 8)):
        element = items.nth(index)
        title = read_item_title(element)
        if check_target_name(title, {norm(target)}) or norm(title) == norm(target):
            element.click()
            logger.info(f"账号 {username} 通过搜索选中 {title}")
            return True
    if items.count():
        items.first.click()
        logger.info(f"账号 {username} 搜索后点击第一条结果")
        return True
    return False


def scroll_and_select_user(page, username, targets):
    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")

    seen_titles = set()
    remaining_targets = set(targets)
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10

    while True:
        target_elements = page.locator(CONVERSATION_ITEM_SELECTOR).all()
        prev_seen = len(seen_titles)

        for element in target_elements:
            try:
                target_name = read_item_title(element)
                if title_is_placeholder(target_name):
                    continue
                if target_name in seen_titles:
                    continue
                seen_titles.add(target_name)
                logger.debug(f"账号 {username} 找到好友 {target_name}")

                target_symbol = check_target_name(target_name, targets)
                if not target_symbol:
                    continue

                element.click()
                yield target_symbol
                if target_symbol in remaining_targets:
                    remaining_targets.remove(target_symbol)
                matched_title = norm(target_name)
                if matched_title in remaining_targets:
                    remaining_targets.remove(matched_title)
                if not remaining_targets:
                    logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                    return
                break
            except Exception:
                traceback.print_exc()
        else:
            new_found = len(seen_titles) > prev_seen
            empty_scroll_count = 0 if new_found else empty_scroll_count + 1

            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                logger.warning(
                    f"账号 {username} 连续 {MAX_EMPTY_SCROLLS} 次滚动未发现新好友，判定已到达底部"
                )
                break

            scrollable_element = page.locator(CONVERSATION_LIST_SELECTOR).element_handle()
            if not scrollable_element:
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                break

            scroll_top_before = page.evaluate("(element) => element.scrollTop", scrollable_element)
            page.evaluate("(element) => element.scrollTop += 800", scrollable_element)
            time.sleep(0.4)
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
            time.sleep(1.2)

    for leftover in list(remaining_targets):
        logger.info(f"账号 {username} 列表未命中 {leftover}，尝试搜索框")
        if search_and_select_user(page, username, leftover):
            yield leftover
            remaining_targets.discard(leftover)

    if remaining_targets:
        logger.warning(f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}")


def is_send_response(response):
    return SEND_API_HINT in (response.url or "") and response.request.method == "POST"


def focus_chat_editor(page):
    editor = page.locator(CHAT_EDITOR_SELECTOR).first
    editor.wait_for(state="visible", timeout=config["browserTimeout"])
    editor.click()
    input_area = page.locator(CHAT_INPUT_AREA_SELECTOR).first
    if input_area.count():
        try:
            input_area.click()
        except Exception:
            pass
    return editor


def type_message(editor, message):
    lines = message.split("\\n")
    if len(lines) == 1:
        lines = message.split("\n")
    for index, line in enumerate(lines):
        if line:
            editor.press_sequentially(line, delay=30)
        if index != len(lines) - 1:
            editor.press("Shift+Enter")


def click_send_button(page):
    candidates = [
        page.get_by_role("button", name="发送"),
        page.locator("[data-e2e='msg-input'] button").last,
        page.locator("button:has-text('发送')"),
    ]
    for locator in candidates:
        try:
            if locator.count() and locator.first.is_visible():
                locator.first.click()
                return True
        except Exception:
            continue
    return False


def send_chat_message(page, username, target_name):
    dismiss_interruptions(page)
    editor = focus_chat_editor(page)
    message = build_message()
    logger.debug(f"账号 {username} 准备发送消息给好友 {target_name}：\n\t{message}")

    type_message(editor, message)
    before = len(send_results)
    try:
        with page.expect_response(is_send_response, timeout=20000):
            editor.press("Enter")
    except PlaywrightTimeoutError:
        logger.warning(f"账号 {username} 回车后未捕获发送请求，尝试点发送按钮")
        if not click_send_button(page):
            editor.press("Enter")
        try:
            page.wait_for_event("response", predicate=is_send_response, timeout=15000)
        except PlaywrightTimeoutError:
            dump_page_debug(page, username, "send_no_request")
            raise RuntimeError(f"账号 {username} 给 {target_name} 发送时没有出现 {SEND_API_HINT} 请求")

    time.sleep(0.8)
    if page.get_by_text("消息发送失败").count():
        dump_page_debug(page, username, "send_failed_toast")
        raise RuntimeError(f"账号 {username} 给 {target_name} 发送失败：页面提示消息发送失败")
    if page.get_by_text("系统繁忙").count():
        dump_page_debug(page, username, "send_busy")
        raise RuntimeError(f"账号 {username} 给 {target_name} 发送失败：系统繁忙 / 身份校验未通过")

    latest = send_results[before:] or send_results[-1:]
    if not latest or latest[-1]["status"] != 200:
        status = latest[-1]["status"] if latest else "无"
        dump_page_debug(page, username, "send_bad_status")
        raise RuntimeError(f"账号 {username} 给 {target_name} 发送接口状态码 {status}")

    logger.info(f"账号 {username} 给好友 {target_name} 发送成功，状态码 200")
    time.sleep(1.5)
    dismiss_interruptions(page)


def new_logged_in_page(browser, cookies):
    context = browser.new_context(
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1440, "height": 900},
        user_agent=USER_AGENT,
    )
    context.set_default_navigation_timeout(config["browserTimeout"])
    context.set_default_timeout(config["browserTimeout"])
    context.add_init_script(HIDE_WEBDRIVER_SCRIPT)
    page = context.new_page()
    page.on("response", handle_response)
    cookies = sanitize_cookies(cookies)
    context.add_cookies(cookies)
    return context, page, cookies


def do_user_task(browser, username, cookies, targets):
    reset_runtime_state()
    context, page, cookies = new_logged_in_page(browser, cookies)
    logger.info(f"账号 {username} 注入 {len(cookies)} 条 cookie")

    try:
        open_official_chat(page, username)
        logger.debug(f"账号 {username} 开始发送消息")
        sent = 0
        for target_name in scroll_and_select_user(page, username, targets):
            logger.debug(f"账号 {username} 已选中好友 {target_name} 发送消息")
            send_chat_message(page, username, target_name)
            sent += 1
        if sent == 0:
            dump_page_debug(page, username, "no_target_sent")
            raise RuntimeError(f"账号 {username} 没有成功选中任何目标好友，未发送")
        logger.info(f"账号 {username} 本轮成功发送 {sent} 条")
    finally:
        context.close()


def runTasks():
    playwright, browser = get_browser()
    try:
        logger.info("开始执行任务")
        logger.debug("当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        if not userData:
            raise RuntimeError("没有可用任务：请检查 TASKS 和对应的 COOKIES_<抖音号> 是否已配置")
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
