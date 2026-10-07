"""每账号一个浏览器线程，模型请求交给独立线程，持续泵 Playwright 事件。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import os
import threading
import time

from core.ai.config import AIConfig
from core.ai.engine import ReplyEngine
from core.ai.providers import create_provider
from core.douyin_im import DouyinIM, STATUS_READY, norm


def resolve_targets(im, targets, stop):
    wanted = {norm(t) for t in targets if norm(t)}
    matches = {t: {} for t in wanted}
    for hit in im.iter_conversations():
        if stop.is_set():
            return []
        for key in wanted:
            fields = ("title", "conv_id") if hit.get("is_group") else (
                "remark", "nickname", "douyin_id", "uid", "sec_uid", "title", "conv_id")
            if any(norm(hit.get(field) or "") == key for field in fields):
                matches[key][str(hit["conv_id"])] = dict(hit)
    if not (im.last_scan or {}).get("scanned_all"):
        raise ValueError("会话列表未扫描完整，暂不启动陪聊，请增加扫描预算后重试")
    result = {}
    for key, found in matches.items():
        if not found:
            raise ValueError(f"未找到陪聊会话：{key}")
        if len(found) != 1:
            raise ValueError(f"陪聊会话存在重名：{key}，请改用会话 ID；好友也可用抖音号或 UID")
        hit = next(iter(found.values()))
        result[str(hit["conv_id"])] = hit
    return list(result.values())


def run_account(account, config: AIConfig, browser_config, stop: threading.Event, emit):
    from cloakbrowser import launch
    # 不复用 get_browser：它缓存全局环境配置；陪聊使用界面启动时的快照。
    browser = context = im = provider = None
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-api")
    name = account.username or account.unique_id
    started_at = time.time()
    try:
        provider = create_provider(config)
        args = ["--no-first-run", "--no-default-browser-check"]
        if account.fingerprint:
            args.append(f"--fingerprint={account.fingerprint}")
        kwargs = {"headless": True, "humanize": True, "args": args}
        if browser_config.proxy_address:
            kwargs["proxy"] = browser_config.proxy_address
        emit("status", f"{name}：正在连接抖音")
        browser = launch(**kwargs)
        context = browser.new_context()
        import json
        cookies = json.loads(account.cookies)
        context.add_cookies([{k: v for k, v in cookie.items() if k != "sameSite"}
                            for cookie in cookies])
        context.set_default_timeout(browser_config.browser_action_timeout * 1000)
        context.set_default_navigation_timeout(browser_config.browser_action_timeout * 1000)
        page = context.new_page()
        im = DouyinIM(page, timeout=browser_config.im_scan_timeout,
                      ready_timeout=browser_config.im_ready_timeout,
                      settle_ms=browser_config.friend_list_wait_time * 1000,
                      max_steps=browser_config.im_max_steps)
        if im.wait_ready().get("status") != STATUS_READY:
            raise ValueError(f"{name} 登录不可用，请刷新登录信息")
        emit("status", f"{name}：已连接，正在扫描好友和群聊会话")
        hits = resolve_targets(im, account.ai_targets, stop)
        engine = ReplyEngine(config, started_at=started_at)
        emit("status", f"{name}：正在初始化会话，跳过现有消息")
        for hit in hits:
            if stop.is_set():
                return
            if not im.select_conversation(hit['conv_id']):
                raise ValueError(f"无法初始化会话：{hit['display']}，请重新启动陪聊")
            page.wait_for_timeout(600)
            engine.seed(str(hit['conv_id']), im.read_chat_messages(str(hit['conv_id'])))
        emit("status", f"{name}：已监听 {len(hits)} 个会话（含 {sum(bool(h.get('is_group')) for h in hits)} 个群聊），仅回复监听就绪后的新文字消息")
        while not stop.is_set():
            for hit in hits:
                if stop.is_set():
                    break
                cid = str(hit["conv_id"])
                if not im.ready:
                    raise ValueError(f"{name} 登录已失效，请刷新登录信息")
                if not im.select_conversation(hit["conv_id"]):
                    continue
                page.wait_for_timeout(600)
                pending = engine.prepare(cid, im.read_chat_messages(cid), is_group=bool(hit.get("is_group")))
                if pending is None:
                    continue
                emit("status", f"{name} → {hit['display']}：正在生成回复")
                future = executor.submit(provider.reply, engine.messages(pending))
                while not future.done() and not stop.is_set():
                    page.wait_for_timeout(150)
                if stop.is_set():
                    break
                try:
                    reply = future.result()
                except Exception as exc:
                    engine.failed(pending)
                    # 仅允许我们自己的安全错误文本出现在事件中。
                    from core.ai.providers import ProviderError
                    message = str(exc) if isinstance(exc, ProviderError) else "AI 生成失败"
                    emit("error", f"{name}：{message}，60 秒后再试")
                    continue
                # API 等待期间有新消息或手动回复，丢弃过时结果，下轮重新判断。
                fresh = engine.prepare(cid, im.read_chat_messages(cid), is_group=bool(hit.get("is_group")))
                if fresh is None or fresh.ids != pending.ids or stop.is_set():
                    continue
                # 发送前就去重；回执缺失不重发，防止同一回复发送两次。
                engine.consume(pending)
                result = im.type_and_send(hit, reply, log_content=False)
                if result.get("ok"):
                    engine.commit(pending, reply)
                    emit("sent", f"{name} → {hit['display']}：回复成功")
                else:
                    emit("error", f"{name} → {hit['display']}：未确认发送成功，未自动重发")
            remaining = config.poll_interval * 1000
            while remaining > 0 and not stop.is_set():
                page.wait_for_timeout(min(remaining, 150))
                remaining -= 150
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        for resource, method in ((provider, "close"), (im, "detach"),
                                 (context, "close"), (browser, "close")):
            if resource is not None:
                with suppress(Exception):
                    getattr(resource, method)()


def cli():
    from app.config.env_store import load_config
    from app.ai_chat import ChatController
    from app.paths import browser_binary
    binary = browser_binary()
    if binary.is_file():
        os.environ.setdefault("CLOAKBROWSER_BINARY_PATH", str(binary))
    os.environ.setdefault("CLOAKBROWSER_AUTO_UPDATE", "false")
    config, _ = load_config()
    controller = ChatController(lambda kind, message: print(message, flush=True))
    try:
        controller.start(config)
        while controller.status()["running"]:
            time.sleep(0.3)
        return 1 if controller.status()["errors"] else 0
    except KeyboardInterrupt:
        print("正在停止陪聊…", flush=True)
        return 0
    except ValueError as exc:
        print(str(exc), flush=True)
        return 1
    finally:
        controller.stop()
        controller.wait()
