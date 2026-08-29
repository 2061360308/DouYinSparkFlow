import traceback
import os
import datetime
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from utils import norm
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser, new_context
from playwright.sync_api import Response
import time

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))

CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"


def handle_response(response: Response, userIDDict):
    """
    只监听你要的那个接口响应
    :param response: Playwright Response 对象
    :param userIDDict: 好友映射字典（每个账号任务独立）
    """
    # 精准匹配目标接口 URL
    if "aweme/v1/web/im/user/info" in response.url:
        try:
            # 获取接口返回的 JSON 数据
            json_data = response.json()
            if not isinstance(json_data, dict):
                return
            data = json_data.get("data") or []
            if not isinstance(data, list):
                return
            for item in data:
                if not isinstance(item, dict):
                    continue
                short_id = item.get("short_id")  # short_id
                unique_id = item.get("unique_id")  # unique_id
                sec_uid = item.get("sec_uid", "")  # sec_uid 可能不存在，提供默认值为空字符串
                nickname = norm(item.get("nickname"))  # 昵称
                remark_name = norm(item.get("remark_name", nickname))  #  备注名，如果没有则使用昵称
                userIDDict[remark_name] = [short_id, unique_id, sec_uid, nickname, remark_name]
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            last = tb[-1]
            print(f"解析响应失败: {e}")
            print(f"文件: {last.filename}, 行号: {last.lineno}, 函数: {last.name}")


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """
    通用的重试逻辑
    :param name: 操作名称（用于日志记录）
    :param operation: 要执行的异步操作
    :param retries: 最大重试次数
    :param delay: 每次重试之间的延迟（秒）
    :param args: 传递给操作的参数
    :param kwargs: 传递给操作的关键字参数
    """
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


def find_record_for_title(title, userIDDict):
    """根据会话窗口显示的名称，找到对应的好友记录（含昵称/备注名兜底）。
    返回 [short_id, unique_id, sec_uid, nickname, remark_name] 或 None。
    """
    title = norm(title)
    if title in userIDDict:
        return userIDDict[title]
    # 显示名可能是昵称而不是备注名，做一次兜底
    for record in userIDDict.values():
        if record and title in (record[3], record[4]):  # (nickname, remark_name)
            return record
    return None


def checkTargetName(targetName, targets, userIDDict):
    """检查 targetName（会话窗口显示名）是否为目标好友。

    由于 targets 可能是 抖音号(unique_id)/短ID(short_id)/昵称/备注名，
    这里先从显示名解析出好友记录，再判断该记录里是否含有目标标识。
    """
    targetName = norm(targetName)

    record = find_record_for_title(targetName, userIDDict)
    if record is not None:
        # record[short_id, unique_id, sec_uid, nickname, remark_name]
        for field in (record[1], record[0], record[3], record[4]):
            if field and field in targets:
                return field

    # 直接匹配：显示名本身就是目标（例如目标配置为昵称）
    if targetName in targets:
        return targetName

    return None


def is_login_page(page, username):
    """检测当前页面是否处于登录状态。无头环境下 cookie 可能被平台判定无效而踢下线，
    此时页面会渲染登录表单（扫码登录/验证码登录），好友列表无法加载。"""
    try:
        body_text = page.locator("body").inner_text()
        markers = ["扫码登录", "验证码登录", "密码登录", "登录后免费畅享"]
        if any(m in body_text for m in markers):
            logger.warning(
                f"账号 {username} 检测到页面处于未登录状态（可能 cookie 已失效或被平台踢下线），"
                f"请重新获取该账号的最新 cookie。"
            )
            return True
    except Exception as e:
        logger.debug(f"账号 {username} 检测登录状态失败：{e}")
    return False


def wait_for_friend_list(page, userIDDict, username, timeout_ms):
    """等待好友列表渲染完成且 im/user/info 接口已填充匹配映射。

    无头环境下好友列表为懒加载，实测需要约 12 秒才渲染完成；且
    im/user/info 接口晚于会话项出现。这里采用轮询而不是一次性
    wait_for_selector，避免被配置里过小的 friendListTimeout 提前中断。
    """
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        # 登录表单可能延迟出现，轮询期间再次检测
        if is_login_page(page, username):
            return False
        try:
            items = page.locator(CONVERSATION_ITEM_SELECTOR).count()
        except Exception:
            items = 0
        if items > 0 and len(userIDDict) > 0:
            logger.debug(
                f"账号 {username} 好友列表已就绪（{items} 个会话，匹配映射 {len(userIDDict)} 条）"
            )
            return True
        time.sleep(1)
    logger.warning(f"账号 {username} 等待好友列表完成超时（{timeout_ms}ms）")
    return False


def scroll_and_select_user(page, username, targets, userIDDict):
    """尝试滚动并查找用户名"""
    # 定义目标元素和滚动容器的选择器
    target_selector = CONVERSATION_ITEM_SELECTOR
    scrollable_friends_selector = CONVERSATION_LIST_SELECTOR

    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")

    found_targets = set()
    # 复制一份目标列表用于追踪进度
    remaining_targets = set(targets)

    # 连续空滚动计数器（滚动后没有发现新好友的次数）
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 12  # 连续12次滚动没有新好友，认为到底了
    MAX_TOTAL_SCROLLS = 60  # 总共最多滚动60次，防止极端情况死循环

    total_scrolls = 0
    while True:
        # 查找所有目标元素
        try:
            target_elements = page.locator(target_selector).all()
        except Exception:
            target_elements = []

        # 记录本轮循环前已发现的好友数，用于判断是否有新发现
        prev_found_count = len(found_targets)

        for element in target_elements:
            try:
                # 查找子元素 span，模糊匹配 class
                span = element.locator(CONVERSATION_TITLE_SELECTOR)
                targetName = span.inner_text()

                if targetName in found_targets:
                    continue  # 已处理过，跳过
                found_targets.add(targetName)

                logger.debug(f"账号 {username} 找到好友 {targetName}")

                targetSymbol = checkTargetName(targetName, targets, userIDDict)

                if targetSymbol:
                    element.click()

                    yield targetSymbol

                    # 标记已找到，如果全找到了直接退出
                    if targetSymbol in remaining_targets:
                        remaining_targets.remove(targetSymbol)
                    if len(remaining_targets) == 0:
                        logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                        return
                    break
            except Exception as e:
                traceback.print_exc()
        else:
            # 检查本轮是否有新好友被发现
            new_found = len(found_targets) > prev_found_count
            if new_found:
                empty_scroll_count = 0  # 有新发现，重置计数器
            else:
                empty_scroll_count += 1  # 无新发现，递增计数器

            # 检查连续空滚动次数，防止死循环
            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                logger.warning(
                    f"账号 {username} 连续 {MAX_EMPTY_SCROLLS} 次滚动未发现新好友，判定已到达底部"
                )
                if len(remaining_targets) > 0:
                    logger.warning(
                        f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
                    )
                break

            # 累计滚动次数保护
            if total_scrolls >= MAX_TOTAL_SCROLLS:
                logger.warning(f"账号 {username} 已达到最大滚动次数 {MAX_TOTAL_SCROLLS}，停止搜索")
                if len(remaining_targets) > 0:
                    logger.warning(
                        f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
                    )
                break

            # 滚动容器
            scrollable_element = page.locator(scrollable_friends_selector).element_handle()

            if scrollable_element:
                # 记录滚动前的 scrollTop，用于检测是否真的滚动了
                try:
                    scroll_top_before = page.evaluate(
                        "(element) => element.scrollTop", scrollable_element
                    )

                    page.evaluate("(element) => element.scrollTop += 800", scrollable_element)

                    # 检测滚动后的 scrollTop
                    time.sleep(0.3)
                    scroll_top_after = page.evaluate(
                        "(element) => element.scrollTop", scrollable_element
                    )
                except Exception as e:
                    logger.warning(f"账号 {username} 滚动好友列表异常：{e}")
                    break

                if scroll_top_before == scroll_top_after:
                    # scrollTop 没有变化，说明已经到底了
                    empty_scroll_count += 2  # 加速判定到底
                    logger.debug(
                        f"账号 {username} scrollTop 未变化 ({scroll_top_before})，可能已到底 (空滚动计数: {empty_scroll_count}/{MAX_EMPTY_SCROLLS})"
                    )
                else:
                    logger.debug(
                        f"账号 {username} 滚动好友列表以加载更多好友 (scrollTop: {scroll_top_before} -> {scroll_top_after})"
                    )

                total_scrolls += 1
                time.sleep(1.5)
            else:
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                break


def dismiss_login_dialog(page):
    """
    关闭可能出现的"保存登录信息"弹窗。

    在无头/全新浏览器环境中，抖音聊天页首次加载会弹出
    "是否保存登录信息？"弹窗，它挡在好友列表前面。若不关闭，
    好友列表不会渲染、im/user/info 接口也不会触发，导致"找不到好友"。
    """
    for selector, label in [
        ("text=取消", "取消"),
        ("text=保存", "保存"),
        ("text=不再提示", "不再提示"),
    ]:
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.click(timeout=3000)
                logger.debug(f"已关闭登录弹窗：{label}")
                time.sleep(1)
                return
        except Exception as e:
            logger.debug(f"尝试关闭弹窗 {label} 失败：{e}")


SCREENSHOT_DIR = "screenshots"


def save_screenshot(page, target_username):
    """发送消息成功后，截屏浏览器内容，保存到 ./screenshots/<当前日期>/ 目录。

    目录命名示例：./screenshots/2026-8-29/，文件名含好友标识与时间戳，避免覆盖。
    """
    try:
        now = datetime.datetime.now()
        date_str = f"{now.year}-{now.month}-{now.day}"
        dir_path = os.path.join(SCREENSHOT_DIR, date_str)
        os.makedirs(dir_path, exist_ok=True)

        ts = now.strftime("%H%M%S")
        safe_name = "".join(c for c in str(target_username) if c.isalnum() or c in "-_") or "friend"
        filename = os.path.join(dir_path, f"{safe_name}_{ts}.png")

        page.screenshot(path=filename, full_page=False)
        logger.info(f"已保存消息截图: {os.path.abspath(filename)}")
    except Exception as e:
        logger.warning(f"保存消息截图失败：{e}")


def send_message(page, target_username):
    """向已选中的好友发送续火花消息。发送逻辑独立，便于多次尝试复用。"""
    # 等待聊天输入框元素加载完成，使用更稳定的属性选择器
    chat_input_selector = CHAT_EDITOR_SELECTOR
    page.wait_for_selector(chat_input_selector, timeout=config["browserTimeout"])
    chat_input = page.locator(chat_input_selector)

    # 在聊天输入框中输入内容
    message = build_message()
    for line in message.split("\\n"):
        chat_input.type(line)  # 输入每一行
        # 如果不是最后一行，模拟 Shift+Enter 插入换行
        if line != message.split("\\n")[-1]:
            chat_input.press("Shift+Enter")  # 模拟 Shift+Enter 插入换行

    logger.debug(f"准备发送消息给好友 {target_username}：\n\t{message}")
    # 模拟按下回车键发送消息
    chat_input.press("Enter")
    time.sleep(2)  # 发送完等待一会儿
    # 发送成功后截屏留档
    save_screenshot(page, target_username)
    logger.debug(f"给好友 {target_username} 发送消息完成")


def do_user_task(browser, username, cookies, targets):
    context = new_context(browser)  # 每个任务使用独立的上下文（带反自动化检测）
    context.set_default_navigation_timeout(
        config["browserTimeout"]
    )  # 设置导航超时时间
    context.set_default_timeout(
        config["browserTimeout"]
    )  # 设置所有操作的默认超时时间

    page = context.new_page()

    # 每个账号任务使用独立的好友映射，避免多账号串行时残留
    userIDDict = {}

    page.on(
        "response",
        lambda response: handle_response(response, userIDDict),
    )  # 监听响应，收集好友完整信息用于匹配

    # 注入 Cookie
    context.add_cookies(cookies)

    wait_timeout = max(config.get("friendListTimeout", 20000), 30000)
    max_attempts = max(config.get("taskRetryTimes", 3), 1)
    target_set = set(targets)

    for attempt in range(1, max_attempts + 1):
        userIDDict.clear()
        try:
            # 打开抖音网页聊天页面
            retry_operation(
                "打开抖音网页聊天页面",
                page.goto,
                retries=2,
                delay=5,
                url="https://www.douyin.com/chat",
            )

            # 关闭可能出现的"保存登录信息"弹窗
            dismiss_login_dialog(page)

            # 检测是否因 cookie 失效被踢下线
            if is_login_page(page, username):
                context.close()
                return

            # 无头环境下好友列表懒加载耗时较长（实测约 12s），且匹配依赖
            # im/user/info 接口数据。轮询等待直到列表就绪，超时时间取
            # friendListTimeout 与 30s 中的较大值，避免过小的配置值打断等待。
            ready = wait_for_friend_list(page, userIDDict, username, wait_timeout)

            # 等待期间登录表单可能延迟出现，若仍未登录则放弃该账号，避免误发
            if is_login_page(page, username):
                context.close()
                return

            time.sleep(2)  # 再给列表内容与 im/user/info 接口一点缓冲时间

            logger.debug(
                f"账号 {username} 开始发送消息（第 {attempt}/{max_attempts} 次尝试, ready={ready}, 映射={len(userIDDict)}）"
            )

            # 滚动并选择用户，逐个发送
            sent = set()
            for target_username in scroll_and_select_user(page, username, targets, userIDDict):
                logger.debug(f"账号 {username} 已选中好友 {target_username} 发送消息")
                send_message(page, target_username)
                sent.add(target_username)

            # 若本轮已把全部目标发完，成功退出
            if sent and sent == target_set:
                logger.info(f"账号 {username} 全部目标好友发送完成")
                break

            logger.warning(
                f"账号 {username} 第 {attempt} 次尝试仅发送 {len(sent)}/{len(target_set)}，"
                f"剩余未发送: {sorted(target_set - sent)}，将重新加载页面重试"
            )
        except Exception as e:
            logger.warning(f"账号 {username} 第 {attempt} 次尝试异常：{e}")
            if attempt >= max_attempts:
                raise

    context.close()  # 任务完成后关闭上下文


def runTasks():
    playwright, browser = get_browser()
    try:
        logger.info("开始执行任务")
        logger.debug(f"当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(
                f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}"
            )

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            # 创建任务
            do_user_task(browser, username, cookies, targets)
            logger.info(f"账号 {username} 任务完成")
    finally:
        # 关闭浏览器实例
        browser.close()

        playwright.stop()
