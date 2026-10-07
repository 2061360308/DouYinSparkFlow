"""Optional browser smoke checks using the built UI and synthetic account data."""
import json
import unittest
from urllib.parse import quote
from pathlib import Path

from app.paths import BUNDLE_DIR
from core.chat_export import JS_HISTORY_SCROLL


class ExportBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = BUNDLE_DIR / 'cloakbrowser-windows-x64' / 'chrome.exe'
        cls.ui = BUNDLE_DIR / 'web' / 'dist' / 'index.html'
        if not binary.is_file() or not cls.ui.is_file():
            raise unittest.SkipTest('Requires bundled Chromium and built UI')
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=str(binary), headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_chat_scroll_uses_message_ancestor_and_overlapping_steps(self):
        page = self.browser.new_page()
        try:
            html = '''<div id="sidebar" style="overflow-y:auto;height:100px"><div style="height:900px">好友</div></div>
              <div id="chat" style="overflow-y:auto;height:200px"><div style="height:1000px">
              <div data-e2e="msg-item-content">文字</div></div></div>'''
            page.goto('data:text/html;charset=utf-8,' + quote(html))
            bottom = page.evaluate(JS_HISTORY_SCROLL, 'bottom')
            older = page.evaluate(JS_HISTORY_SCROLL, 'older')
            self.assertEqual(bottom['top'], 800)
            self.assertEqual(older['top'], 690)
            self.assertEqual(page.locator('#sidebar').evaluate('(e) => e.scrollTop'), 0)
        finally:
            page.close()

    def test_export_button_stop_result_and_directory_flow(self):
        page = self.browser.new_page(viewport={'width': 1100, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        data = {'config': {'accounts': [{'username': '测试账号', 'unique_id': 'synthetic',
                 'targets': [], 'cookies': '', 'conversations': ['测试好友']} ]},
                'schedule': {'mode': 'config'}, 'proxy': {}, 'options': {}, 'env_map': {},
                'issues': [], 'notes': [], 'orphans': []}
        script = '''window.testCalls = [];
          window.$py = async (method, payload) => {
            window.testCalls.push({method, payload});
            if (method === 'get_config') return STATE;
            if (method === 'account_chat_export_start') return {ok:true, session:'synthetic-session'};
            return {ok:true};
          };'''.replace('STATE', json.dumps(data, ensure_ascii=False))
        page.add_init_script(script)
        try:
            page.goto(self.ui.resolve().as_uri())
            page.get_by_text('账户配置', exact=True).click()
            start = page.get_by_role('button', name='导出 TXT + JSON', exact=True)
            self.assertFalse(start.is_enabled())
            page.get_by_role('combobox').nth(1).fill('测试好友')
            page.get_by_role('option', name='测试好友', exact=True).click()
            start.click()
            page.get_by_role('button', name='停止并保存已读记录', exact=True).wait_for()
            page.evaluate("window.__pyOn('browser_event', {session:'synthetic-session', kind:'export_progress', data:{count:42}})")
            page.get_by_text('正在加载历史文字消息… 已读到 42 条', exact=True).wait_for()
            page.get_by_role('button', name='停止并保存已读记录', exact=True).click()
            page.get_by_role('button', name='正在停止并保存…', exact=True).wait_for()
            result = {'folder': 'C:/synthetic/chat_exports/test', 'metadata': {
                'message_count': 42, 'first_message_at': '2026-01-01T10:00:00+08:00',
                'last_message_at': '2026-10-07T10:00:00+08:00', 'stop_description': '用户停止，保留已读取的记录'}}
            page.evaluate("p => window.__pyOn('browser_event', p)",
                          {'session':'synthetic-session', 'kind':'chat_exported', 'data':result})
            page.evaluate("window.__pyOn('browser_event', {session:'synthetic-session', kind:'closed', data:{}})")
            page.get_by_text('已导出 42 条文字消息（未确认全部历史）。', exact=True).wait_for()
            page.locator('.session-panel').get_by_role('button', name='打开导出目录', exact=True).click()
            calls = page.evaluate('window.testCalls')
            start_call = next(c for c in calls if c['method'] == 'account_chat_export_start')
            self.assertEqual(start_call['payload'], {'unique_id':'synthetic', 'friend':'测试好友'})
            self.assertTrue(any(c['method'] == 'account_chat_export_stop' for c in calls))
            self.assertTrue(any(c['method'] == 'chat_exports_open' for c in calls))
            screenshot = BUNDLE_DIR.parent / '.workbuddy' / 'chat-export-preview.png'
            screenshot.parent.mkdir(exist_ok=True)
            page.screenshot(path=str(screenshot))
            page.get_by_role('button', name='关闭', exact=True).click()
            self.assertEqual(page.locator('.session-panel').count(), 0)
            self.assertEqual(errors, [])
        finally:
            page.close()
