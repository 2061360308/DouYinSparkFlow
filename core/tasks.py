import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
from playwright.sync_api import Response
import time
import json


complates = {}

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
matchMode = config.get("matchMode", "nickname")
userIDDict = {}

def handle_response(response: Response):
    """
    只监听你要的那个接口响应
    """
    global userIDDict
    # 精准匹配目标接口 URL
    if "aweme/v1/creator/im/user_detail/" in response.url:
        # print(f"URL: {response.url}")
        # print(f"状态码: {response.status}")
        try:
            # 获取接口返回的 JSON 数据（就是你在 Network 里看到的内容）
            json_data = response.json()
            # print("\n📦 响应 JSON 数据：")
            # print(json.dumps(json_data, indent=4, ensure_ascii=False))
            for item in json_data.get("user_list", []):
                short_id = item.get("user", {}).get("ShortId")
                nickname = item.get("user", {}).get("nickname")
                user_id = item.get("user_id", "")
                userIDDict[str(short_id)] = {"nickname": nickname, "user_id": user_id}
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


def scroll_and_select_user(page, username, targets):
    """尝试滚动并查找用户名"""
    # 定义目标元素和滚动容器的选择器
    friends_tab_selector = 'xpath=//*[@id="sub-app"]/div/div/div[1]/div[2]'
    target_selector = 'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]//div[contains(@class, "semi-list-item-body semi-list-item-body-flex-start")]'
    scrollable_friends_selector = 'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]/div/div/div[3]/div/div/div/ul/div'
    
    no_more_selector = 'xpath=//div[contains(@class, "no-more-tip-")]'
    loading_selector = 'xpath=//div[contains(@class, "semi-spin")]'

    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")

    # 【截图03】进入消息页面后，点击好友页签前
    page.screenshot(path=f"logs/03_before_click_friends_tab_{username}.png", full_page=True)

    logger.debug(f"账号 {username} 点击进入好友标签页")
    try:
        page.wait_for_selector(friends_tab_selector, timeout=30000)
        page.locator(friends_tab_selector).click()
        time.sleep(2) # 等待页面切换
        # 【截图04】点击好友标签页后
        page.screenshot(path=f"logs/04_clicked_friends_tab_{username}.png", full_page=True)
    except Exception as e:
        logger.error(f"账号 {username} 找不到好友标签页元素，页面可能结构已改变: {e}")
        page.screenshot(path=f"logs/debug_friends_tab_error_{username}.png", full_page=True)
        raise

    logger.debug(f"账号 {username} 进入好友列表页面")

    first_friend_selector = 'xpath=//*[@id="sub-app"]/div/div/div[2]/div[2]/div/div/div[1]/div/div/div/ul/div/div/div[1]/li/div'
    try:
        page.wait_for_selector(first_friend_selector, timeout=30000)
        page.locator(first_friend_selector).click()  # 点击第一个好友，确保列表激活
        time.sleep(2)
        # 【截图05】点击第一个好友激活列表后
        page.screenshot(path=f"logs/05_clicked_first_friend_{username}.png", full_page=True)
    except Exception as e:
        logger.error(f"账号 {username} 找不到好友列表元素，XPath 选择器可能已失效: {e}")
        page.screenshot(path=f"logs/debug_first_friend_error_{username}.png", full_page=True)
        raise

    logger.debug(f"账号 {username} 已激活好友列表，开始滚动查找目标好友")

    time.sleep(config["friendListTimeout"] / 1000)  # 等待好友列表加载

    found_targets = set()
    remaining_targets = set(targets)
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10  

    while True:
        target_elements = page.locator(target_selector).all()
        prev_found_count = len(found_targets)

        for element in target_elements:
            try:
                span = element.locator("""xpath=.//span[contains(@class, "item-header-name-")]""")
                targetName = span.inner_text()

                if targetName in found_targets:
                    continue  
                found_targets.add(targetName)

                logger.debug(f"账号 {username} 找到好友 {targetName}")
                
                if matchMode == "short_id":
                    targetSymbol = next((sid for sid, info in userIDDict.items() if info.get("nickname") == targetName), None)
                else:
                    targetSymbol = targetName

                if targetSymbol in targets:
                    element.click()
                    if matchMode == "short_id":
                        logger.debug(f"账号 {username} 选中目标好友 {targetName} 准备开始交互")
                    else:
                        logger.debug(f"账号 {username} 选中目标好友 {targetName} (ShortId: {targetSymbol}) 准备开始交互")
                    yield targetName
                    
                    if targetSymbol in remaining_targets:
                        remaining_targets.remove(targetSymbol)
                    if len(remaining_targets) == 0:
                        logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                        return
                    break
            except Exception as e:
                traceback.print_exc()
        else:
            new_found = len(found_targets) > prev_found_count
            if new_found:
                empty_scroll_count = 0  
            else:
                empty_scroll_count += 1  

            if page.locator(no_more_selector).count() > 0:
                logger.info(f"账号 {username} 检测到'没有更多了'标志，已到达底部")
                if len(remaining_targets) > 0:
                    logger.warning(f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}")
                break

            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                logger.warning(f"账号 {username} 连续 {MAX_EMPTY_SCROLLS} 次滚动未发现新好友，判定已到达底部")
                if len(remaining_targets) > 0:
                    logger.warning(f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}")
                break

            if page.locator(loading_selector).count() > 0:
                logger.debug(f"账号 {username} 列表正在加载中 (Loading)...")
                time.sleep(1.5) 

            scrollable_element = page.locator(scrollable_friends_selector).element_handle()
            
            if scrollable_element:
                scroll_top_before = page.evaluate("(element) => element.scrollTop", scrollable_element)
                page.evaluate("(element) => element.scrollTop += 800", scrollable_element)
                
                time.sleep(0.3)
                scroll_top_after = page.evaluate("(element) => element.scrollTop", scrollable_element)
                
                if scroll_top_before == scroll_top_after:
                    empty_scroll_count += 2  
                    logger.debug(f"账号 {username} scrollTop 未变化 ({scroll_top_before})，可能已到底")
                else:
                    logger.debug(f"账号 {username} 滚动好友列表以加载更多好友 (scrollTop: {scroll_top_before} -> {scroll_top_after})")
                
                time.sleep(1.5)
            else:
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                break


def do_user_task(browser, username, cookies, targets):
    context = browser.new_context()  
    context.set_default_navigation_timeout(config["browserTimeout"])  
    context.set_default_timeout(config["browserTimeout"])  

    page = context.new_page()
    
    if matchMode == "short_id":  
        page.on("response", handle_response)
    
    # 1. 打开抖音创作者中心
    retry_operation(
        "打开抖音创作者中心",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url="https://creator.douyin.com/",
    )
    time.sleep(2)
    # 【截图00】裸连创作者中心，看有没有要求登录或被风控
    page.screenshot(path=f"logs/00_creator_home_{username}.png", full_page=True)
    
    # 2. 注入 Cookie 并刷新验证
    context.add_cookies(cookies)
    page.reload() # 注入Cookie后必须刷新一下让Cookie生效
    time.sleep(3)
    # 【截图01】确认 Cookie 生效，是否成功登录
    page.screenshot(path=f"logs/01_after_cookie_{username}.png", full_page=True)

    # 3. 导航到消息页面
    retry_operation(
        "导航到消息页面",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url="https://creator.douyin.com/creator-micro/data/following/chat",
    )
    time.sleep(5) # 给聊天页面充足的加载时间
    # 【截图02】确认聊天页面是否白屏，侧边栏有没有加载出来
    page.screenshot(path=f"logs/02_chat_page_{username}.png", full_page=True)

    logger.debug(f"账号 {username} 开始发送消息")
    
    # 4. 滚动并选择用户
    for targetName in scroll_and_select_user(page, username, targets):
        # 保护文件名不包含非法字符
        safe_targetName = targetName.replace("/", "_").replace("\\", "_")
        logger.debug(f"账号 {username} 已选中好友 {targetName} 发送消息")
        
        # 【截图06】点击目标好友后的画面
        page.screenshot(path=f"logs/06_selected_friend_{username}_{safe_targetName}.png", full_page=True)

        chat_input_selector = "xpath=//div[contains(@class, 'chat-input-')]"
        try:
            page.wait_for_selector(chat_input_selector, timeout=config["browserTimeout"])
            chat_input = page.locator(chat_input_selector)
        except Exception as e:
            logger.error(f"账号 {username} 找不到聊天输入框: {e}")
            # 【截图07】极其关键：找不到输入框时立刻截图保留现场
            page.screenshot(path=f"logs/07_no_chat_input_{username}_{safe_targetName}.png", full_page=True)
            page_content = page.content()
            logger.debug(f"页面 HTML (前 3000 字):\n{page_content[:3000]}")
            continue  

        message = build_message()
        for line in message.split("\\n"):
            chat_input.type(line)  
            if line != message.split("\\n")[-1]:
                chat_input.press("Shift+Enter")  

        logger.debug(f"账号 {username} 准备发送消息给好友 {targetName}：\n\t{message}")
        
        # 【截图08】输入消息后未发送时的状态
        page.screenshot(path=f"logs/08_message_typed_{username}_{safe_targetName}.png")

        chat_input.press("Enter")
        time.sleep(10)  
        
        # 【截图09】发送完毕后的状态
        page.screenshot(path=f"logs/09_message_sent_{username}_{safe_targetName}.png")

        # 👇【新增这里】：在循环结束后，关闭上下文之前，再死等 5 秒，保护最后一个好友的网络请求不断开
    logger.debug(f"账号 {username} 所有消息已发送，等待 5 秒后关闭环境...")
    time.sleep(6)

    context.close()  # 任务完成后关闭上下文

def runTasks():
    playwright, browser = get_browser()
    try:
        # 检查是否启用多任务和任务数量
        # 创建信号量以限制并发任务数量
        logger.info("开始执行任务")
        logger.debug(f"当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}")

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            complates[user["unique_id"]] = []  # 初始化该用户的已完成列表
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            # 创建任务
            do_user_task(browser, username, cookies, targets)
            logger.info(f"账号 {username} 任务完成")
    finally:
        # 关闭浏览器实例
        browser.close()
        
        playwright.stop()

        

