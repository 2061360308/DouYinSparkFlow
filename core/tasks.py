import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.msg_builder import build_message
from core.browser import get_browser
from core.douyin_im import DouyinIM, STATUS_READY, norm


config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))


def do_user_task(browser, username, cookies, targets):
    """一个账号的完整流程：门禁 → 滚动找人 → 发送 → 回执确认。

    实现委托给 `core.douyin_im.DouyinIM`：
      任务一（门禁）    DouyinIM 构造时自动完成，结论在 wait_ready() 里
      任务二（找人）    iter_find_and_select —— yield 时该会话已选中且 conv_id 已校验
      任务三（发送）    im.type_and_send —— 真实键盘事件 + HTTP/DOM 回执双确认
    拟人化节奏由 cloakbrowser 的 humanize 负责，这里不再叠加延迟。
    """
    context = browser.new_context()  # 每个任务使用独立的上下文
    context.set_default_navigation_timeout(
        config["browserActionTimeout"]
    )  # 导航超时（毫秒，config 已换算好）
    context.set_default_timeout(
        config["browserActionTimeout"]
    )  # 单次操作默认超时（毫秒）

    page = context.new_page()

    # 注入 Cookie
    context.add_cookies(cookies)

    im = None
    try:
        # 打开抖音网页聊天页面由库内部完成（先挂钩子再导航，顺序不可颠倒）
        # 扫描参数全部来自配置：总预算/门禁等待是秒，静默窗是毫秒（见 utils.config）
        im = DouyinIM(
            page,
            timeout=config["imScanTimeout"],
            ready_timeout=config["imReadyTimeout"],
            settle_ms=config["friendListSettleMs"],
            max_steps=config["imMaxSteps"],
        )

        res = im.wait_ready()
        if res.get("status") != STATUS_READY:
            # 终端态都要显式打印，方便从日志一眼看出是哪种失败
            reason = {
                "LOGGED_OUT": "未登录（没有 sessionid）",
                "EXPIRED": "登录已失效（有 sessionid 但服务端不认）",
                "LOGIN_LOST": "运行期掉登录",
                "TIMEOUT": "等待超时",
                "ERROR": "内部错误",
            }.get(res.get("status"), res.get("status"))
            logger.error(f"账号 {username} 操作前检查未通过：{reason}，跳过该账号")
            return

        logger.info(
            f"账号 {username} 门禁通过  user_id={res.get('user_id')} "
            f"nickname={res.get('nickname')} 会话列表就绪"
        )

        sent_ok = sent_fail = 0

        # 生成器：yield 出来的那一刻，对应好友的会话已经被选中
        for friend in im.iter_find_and_select(targets):
            logger.debug(f"账号 {username} 已选中好友 {friend['display']}，准备发送")
            message = build_message()
            r = im.type_and_send(friend, message)
            if r["ok"]:
                sent_ok += 1
                logger.info(
                    f"账号 {username} → {friend['display']} 发送成功"
                    f"（{r.get('via')} message_id={r.get('message_id') or '-'}）"
                )
            else:
                sent_fail += 1
                # 重试一次：用 conv_id 重新选中（列表可能已滚动，原来的下标失效）
                logger.warning(
                    f"账号 {username} → {friend['display']} 未拿到回执，重试一次"
                )
                try:
                    if friend.get("reselect") and friend["reselect"]():
                        r2 = im.type_and_send(friend, message)
                        if r2["ok"]:
                            sent_ok += 1
                            sent_fail -= 1
                            logger.info(
                                f"账号 {username} → {friend['display']} 重试成功"
                            )
                except Exception:
                    logger.warning(traceback.format_exc())
            # 发送完让列表状态落定，再继续滚动（发送会把该会话移到顶部）
            page.wait_for_timeout(800)

        scan = im.last_scan or {}
        logger.info(
            f"账号 {username} 扫描结束：停止原因={scan.get('stopped')} "
            f"步数={scan.get('steps')} 访问会话={scan.get('visited')} "
            f"发送成功={sent_ok} 发送失败={sent_fail}"
        )
        if scan.get("missing"):
            # 这两句必须区分开：scanned_all=False 时"没找到"不代表"不存在"
            logger.warning(
                f"账号 {username} 未找到的目标：{scan['missing']}"
                f"（{scan.get('note')}）"
            )
        if scan.get("select_failed"):
            logger.warning(
                f"账号 {username} 找到但选中失败：{scan['select_failed']}"
            )

        folds = im.fold_groups()
        if any(v for v in folds.values() if v):
            logger.warning(
                f"账号 {username} 注意：折叠组/陌生人组里有内容 {folds}，"
                f"主列表扫不到，目标可能被折叠"
            )
    finally:
        if im is not None:
            try:
                im.detach()
            except Exception:
                pass
        context.close()  # 任务完成后关闭上下文


def runTasks():
    # 检查是否启用多任务和任务数量
    # 创建信号量以限制并发任务数量
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
        # 归一化在**这里**做（配置读取端不做）：DouyinIM._match 内部用同一套 norm，
        # 两边都归过才谈得上相等 —— 否则配置里的「Ｌｕ瞳」永远匹配不上页面上的「Lu瞳」。
        # 同时丢掉归一后变空的项：空串留在剩余名单里永远扣不掉，会白滚到底。
        targets = [t for t in map(norm, user["targets"]) if t]
        username = user.get("username", "未知用户")
        fingerprint = user.get("fingerprint", None)
        logger.info(f"开始处理账号 {username}")
        # 创建任务
        try:
            browser = get_browser(fingerprint)
            do_user_task(browser, username, cookies, targets)
            logger.info(f"账号 {username} 任务完成")
        finally:
            # 关闭浏览器实例
            browser.close()
    