"""陪聊行为、协议适配与生命周期：使用本地假服务，不联系真实好友。"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.ai_chat import ChatController
from app.config.models import Account, Config
from app.config import env_store
from core.ai.config import AIConfig, ProviderConfig
from core.ai.engine import ReplyEngine
from core.ai.providers import OpenAICompatibleProvider, ProviderError
from core.ai.runner import resolve_targets, run_account
from core.douyin_im import JS_CHAT_MESSAGES


def ai_config():
    return AIConfig(providers=[ProviderConfig(api_key="test-secret")], cooldown=0)


def row(mid, text="你好", at=101, own=False):
    return {"id": mid, "text": text, "created_at": at, "from_me": own}


class ReplyEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = ReplyEngine(ai_config(), started_at=100)

    def test_history_and_own_messages_are_not_replied(self):
        self.assertIsNone(self.engine.prepare("a", [row("old", at=99)]))
        self.assertIsNone(self.engine.prepare("a", [row("own", own=True)]))
        self.assertIsNone(self.engine.prepare("a", [row("user"), row("manual", at=102, own=True)]))

    def test_batches_new_messages_and_deduplicates_after_sending(self):
        rows = [row("2", "第二句", 102), row("1", "第一句", 101)]
        pending = self.engine.prepare("a", rows)
        self.assertEqual(pending.text, "第一句\n第二句")
        self.engine.commit(pending, "回复", now=103)
        self.assertIsNone(self.engine.prepare("a", rows, now=104))
        self.assertIsNotNone(self.engine.prepare("b", rows, now=104))

    def test_unconfirmed_send_is_consumed_without_polluting_context(self):
        pending = self.engine.prepare("a", [row("1")])
        self.engine.consume(pending)
        self.assertIsNone(self.engine.prepare("a", [row("1")]))
        self.assertEqual(self.engine.history, {})

    def test_cooldown_and_context_are_bounded_and_isolated(self):
        self.engine.config.cooldown = 10
        self.engine.config.context_turns = 1
        first = self.engine.prepare("a", [row("1")], now=102)
        self.engine.commit(first, "回复一", now=102)
        self.assertIsNone(self.engine.prepare("a", [row("2", at=103)], now=103))
        next_ = self.engine.prepare("a", [row("2", at=103)], now=113)
        self.assertEqual(len(self.engine.messages(next_)), 4)
        self.engine.commit(next_, "回复二", now=113)
        self.assertEqual(len(self.engine.history["a"]), 2)
        self.assertNotIn("b", self.engine.history)

    def test_api_error_retries_after_backoff(self):
        pending = self.engine.prepare("a", [row("1")], now=102)
        self.engine.failed(pending, now=102)
        self.assertIsNone(self.engine.prepare("a", [row("1")], now=161))
        self.assertIsNotNone(self.engine.prepare("a", [row("1")], now=163))


class ConfigTests(unittest.TestCase):
    def test_multiple_providers_and_target_lists_roundtrip(self):
        config = Config(ai_chat=ai_config(), accounts=[Account(unique_id="a", targets=["续火"], ai_targets=["陪聊"])])
        config.ai_chat.providers.append(ProviderConfig(id="custom", name="自建服务", kind="openai-compatible", base_url="http://localhost:1234/v1", api_key="local", model="my-model"))
        config.ai_chat.system_prompt = "包含单引号 '、中文、换行\n和双引号 \""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'.env'
            env_store.save_config(config, path)
            loaded, _ = env_store.load_config(path)
            self.assertEqual(loaded.ai_chat.to_dict(), config.ai_chat.to_dict())
            self.assertEqual(loaded.accounts[0].ai_targets, ["陪聊"])
            self.assertEqual(loaded.accounts[0].targets, ["续火"])
        self.assertNotIn("test-secret", repr(config.ai_chat))

    def test_old_config_does_not_opt_friends_into_ai(self):
        config = Config.from_env_map({"TASKS": json.dumps([{"unique_id": "a", "targets": ["旧好友"]}])})
        self.assertEqual(config.accounts[0].ai_targets, [])
        self.assertEqual(config.ai_chat.providers[0].api_key, "")

    def test_validation(self):
        config = ai_config()
        config.providers[0].base_url = 'https://example.com?token=secret'
        with self.assertRaises(ValueError):config.selected()
        with self.assertRaises(ValueError):AIConfig.from_value({'poll_interval': 0})
        with self.assertRaises(ValueError):AIConfig.from_value({'providers': []})

    def test_invalid_config_can_be_repaired_in_ui(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'.env'
            path.write_text('AI_CHAT=not-json\n', encoding='utf-8')
            loaded, notes = env_store.load_config(path)
            self.assertEqual(loaded.ai_chat.active_provider, 'deepseek')
            self.assertTrue(any('AI_CHAT' in n for n in notes))
            self.assertIn('not-json', path.read_text())


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                cls.calls.append((self.path, data, self.headers.get('Authorization')))
                self.send_response(200);self.send_header('Content-Type', 'application/json');self.end_headers()
                self.wfile.write(json.dumps({'id':'test','object':'chat.completion','created':1,'model':data['model'],'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'你好，我是 AI。'}}]}).encode())
            def log_message(self, *_):pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def test_real_sdk_deepseek_and_compatible_requests(self):
        for kind in ['deepseek', 'openai-compatible']:
            config = ai_config();config.providers[0].kind = kind
            config.providers[0].base_url = f'http://127.0.0.1:{self.server.server_port}/v1'
            provider = OpenAICompatibleProvider(config.selected(), config)
            try:self.assertEqual(provider.reply([{'role':'user','content':'测试'}]), '你好，我是 AI。')
            finally:provider.close()
            path, body, auth = self.calls[-1]
            self.assertEqual(path, '/v1/chat/completions');self.assertEqual(auth, 'Bearer test-secret')
            self.assertEqual(body['messages'], [{'role':'user','content':'测试'}])
            self.assertEqual('thinking' in body, kind == 'deepseek')

    def test_errors_do_not_expose_secrets_or_empty_replies(self):
        from openai import AuthenticationError
        import httpx
        config = ai_config();provider = OpenAICompatibleProvider(config.selected(), config)
        try:
            provider.client.chat.completions.create = MagicMock(side_effect=AuthenticationError('test-secret',response=httpx.Response(401,request=httpx.Request('POST','https://example.com')),body={'api_key':'test-secret'}))
            with self.assertRaises(ProviderError) as result:provider.reply([])
            self.assertNotIn('test-secret', str(result.exception))
            provider.client.chat.completions.create = MagicMock(return_value=MagicMock(choices=[]))
            with self.assertRaises(ProviderError):provider.reply([])
        finally:provider.close()


class TargetAndLifecycleTests(unittest.TestCase):
    def test_exact_resolution_rejects_groups_missing_and_ambiguous_names(self):
        im = MagicMock(last_scan={'scanned_all':True})
        im.iter_conversations.return_value = [{'conv_id':'a','title':'小明','is_group':False}]
        self.assertEqual(resolve_targets(im,['小明'],threading.Event())[0]['conv_id'],'a')
        for hits in [[{'conv_id':'a','title':'小明','is_group':True}],
                     [{'conv_id':'a','title':'小明','is_group':False},{'conv_id':'b','title':'小明','is_group':False}],
                     [{'conv_id':'a','title':'小明明','is_group':False}]]:
            im.iter_conversations.return_value=hits
            with self.assertRaises(ValueError):resolve_targets(im,['小明'],threading.Event())

    def test_controller_start_stop_and_no_double_start(self):
        entered = threading.Event()
        def runner(account, cfg, browser_cfg, stop, emit):
            entered.set();stop.wait(2)
        controller = ChatController(runner=runner)
        config = Config(ai_chat=ai_config(),accounts=[Account(username='test',unique_id='a',cookies='[{"name":"sessionid","value":"test","domain":".douyin.com","path":"/"}]',ai_targets=['好友'])])
        with patch('app.ai_chat.create_provider') as factory:
            controller.start(config);self.assertTrue(entered.wait(1))
            with self.assertRaises(ValueError):controller.start(config)
            controller.stop();controller.wait(2)
        self.assertFalse(controller.status()['running'])

    def test_generation_then_stop_never_sends_and_closes_resources(self):
        stop = threading.Event()
        config = Config(ai_chat=ai_config())
        account = Account(username='test',cookies='[]',ai_targets=['好友'])
        im = MagicMock(ready=True,last_scan={'scanned_all':True})
        im.wait_ready.return_value={'status':'READY'}
        hit={'conv_id':'a','display':'好友','title':'好友','is_group':False}
        im.iter_conversations.return_value=[hit]
        im.read_chat_messages.return_value=[row('1',at=time.time()+1)]
        provider = MagicMock()
        def reply(messages):stop.set();return '不得发送'
        provider.reply.side_effect=reply
        with patch('cloakbrowser.launch') as launch, patch('core.ai.runner.DouyinIM',return_value=im),patch('core.ai.runner.create_provider',return_value=provider):
            run_account(account, config.ai_chat,config,stop,lambda *_:None)
            im.type_and_send.assert_not_called()
            launch.return_value.close.assert_called_once()
            provider.close.assert_called_once()

    def test_runner_generates_only_message_context_and_confirms_send(self):
        stop = threading.Event()
        config = Config(ai_chat=ai_config())
        account = Account(username='test',cookies='[]',ai_targets=['好友'])
        im = MagicMock(ready=True,last_scan={'scanned_all':True})
        im.wait_ready.return_value={'status':'READY'}
        hit={'conv_id':'a','display':'好友','title':'好友','is_group':False}
        im.iter_conversations.return_value=[hit]
        im.read_chat_messages.return_value=[row('1',at=time.time()+1)]
        provider = MagicMock();provider.reply.return_value='你好，我是 AI。'
        events = []
        def send(*args, **kwargs):stop.set();return {'ok':True}
        im.type_and_send.side_effect=send
        with patch('cloakbrowser.launch'), patch('core.ai.runner.DouyinIM',return_value=im),patch('core.ai.runner.create_provider',return_value=provider):
            run_account(account,config.ai_chat,config,stop,lambda *e:events.append(e))
        im.type_and_send.assert_called_once_with(hit,'你好，我是 AI。',log_content=False)
        self.assertTrue(any(e[0]=='sent' for e in events))
        self.assertEqual(provider.reply.call_args.args[0][-1],{'role':'user','content':'你好'})


class MessageReaderTests(unittest.TestCase):
    def test_selection_rescans_cached_conversations(self):
        from core.douyin_im import DouyinIM
        im = object.__new__(DouyinIM)
        im._current_conv = lambda: {"convId": "other"}
        target = {"conv_id": "target", "data_index": 0}
        im._read_window = MagicMock(side_effect=[[{"conv_id": "other"}], [target]])
        im._walk = lambda: iter([[]])
        im._select_and_verify = MagicMock(return_value=True)
        self.assertTrue(im.select_conversation("target"))
        im._select_and_verify.assert_called_once_with(target)

    def test_real_browser_filters_message_identity_and_direction(self):
        from app.paths import browser_binary
        if not browser_binary().is_file():self.skipTest('无 Chromium')
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=str(browser_binary()),headless=True)
            try:
                page=browser.new_page()
                page.goto('data:text/html,<div id="messages"></div>')
                page.evaluate('''() => {
                  for (const m of [
                    {serverId:'2',conversationId:'a',createdAt:new Date(102000),content:JSON.stringify({text:'自己'}),isMyMessage:true},
                    {serverId:'1',conversationId:'a',createdAt:new Date(101000),content:JSON.stringify({text:'好友'}),isMyMessage:false},
                    {serverId:'3',conversationId:'b',createdAt:new Date(103000),content:JSON.stringify({text:'错误会话'})},
                    {serverId:'4',conversationId:'a',createdAt:new Date(104000),content:JSON.stringify({url:'视频'})},
                    {serverId:'1',conversationId:'a',createdAt:new Date(101000),content:JSON.stringify({text:'重复渲染'})}
                  ]) {const el=document.createElement('div');el.setAttribute('data-e2e','msg-item-content');el.__reactFiberTest={memoizedProps:{message:m}};document.querySelector('#messages').append(el)}
                }''')
                rows=page.evaluate(JS_CHAT_MESSAGES,'a')
                self.assertEqual([r['id'] for r in rows],['1','2'])
                self.assertTrue(rows[1]['from_me']);self.assertEqual(rows[0]['created_at'],101)
                from core.douyin_im import DouyinIM
                im = object.__new__(DouyinIM)
                im.page = page
                im._current_conv = lambda: {"convId": "a", "index": 0, "title": "好友"}
                self.assertEqual(im.read_chat_messages('a'), rows)
                self.assertEqual(im.read_chat_messages('b'), [])
                self.assertTrue(im.select_conversation('a'))
            finally:browser.close()


if __name__ == '__main__':unittest.main()
