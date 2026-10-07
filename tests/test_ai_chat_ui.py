"""真 Chromium + 构建界面 + Python 桥；AI 和好友发送用假实现。"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.ai_chat import ChatController
from app.config import env_store, profile_store, settings
from app.config.models import Account, Config
from app.paths import browser_binary
from app.web.bridge import Bridge
from app.web.service import Service
from core.ai.config import AIConfig, ProviderConfig

DIST = Path(__file__).resolve().parents[1] / 'app/web/dist/index.html'


@unittest.skipUnless(DIST.is_file() and browser_binary().is_file(), '缺少前端或 Chromium')
class AIChatUITests(unittest.TestCase):
    def test_service_management_config_test_and_start_stop(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(settings, 'SETTINGS_FILE', root/'local.json'), \
                 patch.object(profile_store, 'INDEX_FILE', root/'profiles.json'), \
                 patch('app.ai_chat.create_provider') as factory:
                provider = MagicMock()
                provider.reply.return_value = '你好，我是 AI。'
                factory.return_value = provider
                config = Config(ai_chat=AIConfig(providers=[ProviderConfig(api_key='fake-ui-key')]),
                                accounts=[Account(username='示例账号', unique_id='example',
                                                  cookies='[{"name":"sessionid"}]', fingerprint='12345',
                                                  targets=['续火朋友'], ai_targets=['陪聊朋友'])])
                env_store.save_config(config, root/'.env')
                service = Service(root/'.env')
                entered = threading.Event()
                def runner(account, ai, cfg, stop, emit):
                    entered.set()
                    stop.wait(10)
                chat = ChatController(runner=runner)
                bridge = Bridge()
                bridge.register_all(get_config=service.get_config, save_config=service.save_config,
                                    schedule_status=service.schedule_status, ai_chat_status=chat.status,
                                    ai_chat_test=chat.test, ai_chat_start=lambda _: chat.start(service.config),
                                    ai_chat_stop=chat.stop)
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=str(browser_binary()), headless=True)
                    try:
                        page = browser.new_page(viewport={'width':1200, 'height':900})
                        errors = []
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        page.goto(DIST.as_uri())
                        page.expose_function('$py', bridge.call)
                        page.get_by_role('button', name='AI 陪聊', exact=True).click()
                        self.assertEqual(page.get_by_label('API Key', exact=True).get_attribute('type'), 'password')
                        page.get_by_role('button', name='添加服务', exact=True).click()
                        page.wait_for_function("document.querySelector('#ai-name').value === '新服务'")
                        page.wait_for_timeout(800)
                        self.assertEqual(len(Service(root/'.env').config.ai_chat.providers), 2)
                        page.get_by_role('button', name='删除当前服务', exact=True).click()
                        page.wait_for_timeout(800)
                        page.get_by_role('button', name='测试 API', exact=True).click()
                        page.get_by_text('连接成功：你好，我是 AI。', exact=True).wait_for()
                        self.assertNotIn('fake-ui-key', service.get_config()['env_map']['AI_CHAT'])
                        page.get_by_role('button', name='启动陪聊', exact=True).click()
                        page.get_by_text('运行中', exact=True).wait_for()
                        self.assertTrue(entered.is_set())
                        page.get_by_role('button', name='停止陪聊', exact=True).click()
                        page.get_by_text('未启动', exact=True).wait_for()
                        self.assertEqual(errors, [])
                    finally:
                        chat.stop()
                        chat.wait()
                        browser.close()
