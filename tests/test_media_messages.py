"""真实 Chromium 解析媒体消息；发送及 AI 服务均使用本地假实现。"""
import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from app.config.models import Account, Config
from app.paths import browser_binary
from core.ai.config import AIConfig
from core.ai.engine import ReplyEngine
from core.ai.runner import run_account
from core.douyin_im import JS_CHAT_MESSAGES


@unittest.skipUnless(browser_binary().is_file(), '缺少 Chromium')
class MediaMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=str(browser_binary()), headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def read(self, fixtures, include_media=True):
        page = self.browser.new_page()
        try:
            page.goto('data:text/html,<div id="messages"></div>')
            page.evaluate('''rows => {for (const [i, r] of rows.entries()) {
              const el = document.createElement('div'); el.dataset.e2e='msg-item-content';
              el.innerHTML = r.html || '';
              el.__reactFiberTest = {memoizedProps:{message:{
                serverId:r.id || String(i+1), conversationId:r.conv || 'a',
                createdAt:new Date((101+i)*1000), orderInConversation:String(i+1),
                sender:r.own?'self':'friend', isMyMessage:true,
                content:r.raw || JSON.stringify(r.content)
              }}};
              document.querySelector('#messages').append(el);
            }}''', fixtures)
            return page.evaluate(JS_CHAT_MESSAGES, {'conv_id':'a', 'self_uid':'self', 'include_media':include_media})
        finally:
            page.close()

    def test_emoji_sticker_and_video_have_readable_bounded_summaries(self):
        rows = self.read([
            {'content':{'text':'😂👍'}},
            {'content':{'sticker_id':'123','display_name':'开心','resource_url':'https://example.invalid/secret'}},
            {'content':{'aweme_id':'456','title':'猫咪日常'}},
            {'content':{'emoji':'🥰'}},
            {'content':{'resource_url':'https://example.invalid/image'}},
            {'content':{'itemId':'789','desc':'x'*2000}},
        ])
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]['text'], '😂👍')
        self.assertIn('开心', rows[1]['text'])
        self.assertIn('猫咪日常', rows[2]['text'])
        self.assertIn('未观看视频内容', rows[2]['text'])
        self.assertIn('🥰', rows[3]['text'])
        self.assertIn('表情或图片', rows[4]['text'])
        self.assertLess(len(rows[5]['text']), 550)
        self.assertNotIn('https://', json.dumps(rows))

    def test_dom_labels_nested_cards_unknown_and_text_only_export(self):
        fixtures = [
            {'content':{'sticker':{'name':'比心'}}},
            {'content':{'video':{'aweme_id':'123','desc':'分享美食'}}},
            {'content':{},'html':'<img alt="微笑" src="data:,">'},
            {'content':{},'html':'<a href="https://www.douyin.com/video/123">旅行记录</a>'},
            {'content':{'notice':'系统通知'}},
            {'raw':'invalid json'},
            {'content':{'text':'正常文字'}},
            {'content':{'text':'错会话'},'conv':'b'},
        ]
        rows = self.read(fixtures)
        self.assertEqual([r['id'] for r in rows], ['1','2','3','4','7'])
        for row, label in zip(rows, ['比心','分享美食','微笑','旅行记录','正常文字']):
            self.assertIn(label, row['text'])
        self.assertEqual([r['id'] for r in self.read(fixtures, False)], ['7'])

    def test_actual_share_card_title_fields_and_author_is_not_title(self):
        rows = self.read([
            {'content':{'itemId':'123','content_title':'今天的旅行记录','content_name':'作者名字'}},
            {'content':{'itemId':'456','aweme_title':'分享评论的视频标题','comment':'一条评论'}},
            {'content':{'itemId':'789','content_title':'','content_name':'不能当标题的作者'}},
            {'content':{'video':{'aweme_id':'10','content_title':'嵌套的分享标题'}}},
        ])
        self.assertIn('标题/描述：今天的旅行记录', rows[0]['text'])
        self.assertIn('标题/描述：分享评论的视频标题', rows[1]['text'])
        self.assertNotIn('标题/描述', rows[2]['text'])
        self.assertNotIn('不能当标题的作者', rows[2]['text'])
        self.assertIn('标题/描述：嵌套的分享标题', rows[3]['text'])
        for r in rows:
            self.assertIn('未观看视频内容', r['text'])

    def test_media_keeps_history_direction_dedup_group_and_manual_reply_rules(self):
        fixtures = [{'id':'old','content':{'sticker_id':'old'}},
                    {'id':'sticker','content':{'emoji':'😂'}},
                    {'id':'video','content':{'awemeId':'video'}},
                    {'id':'video','content':{'awemeId':'video'}}]
        rows = self.read(fixtures)
        self.assertEqual([r['id'] for r in rows], ['old','sticker','video'])
        engine = ReplyEngine(AIConfig(cooldown=0), started_at=100)
        engine.seed('a', rows[:1])
        pending = engine.prepare('a', rows, now=110, is_group=True)
        self.assertEqual(pending.ids, ('sticker','video'))
        self.assertIn('[成员1]', pending.text)
        self.assertIn('不要声称看过视频', engine.messages(pending)[0]['content'])
        engine.commit(pending, '回复', now=110)
        self.assertIsNone(engine.prepare('a', rows, now=111))
        own = self.read(fixtures + [{'content':{'sticker_id':'own'},'own':True}])
        self.assertTrue(own[-1]['from_me'])
        fresh = ReplyEngine(AIConfig(cooldown=0), started_at=100)
        fresh.seed('a', rows[:1])
        self.assertIsNone(fresh.prepare('a', own, now=111))

    def test_runner_requests_media_in_seed_poll_and_presend_then_sends(self):
        for content in ({'text':'😂👍'}, {'sticker_id':'123'}, {'aweme_id':'456','title':'猫咪'}):
            with self.subTest(content=content):
                rows = self.read([{'content':content}])
                for row in rows:
                    row['created_at'] = time.time()+1
                stop = threading.Event()
                config = Config(ai_chat=AIConfig(cooldown=0))
                account = Account(username='测试', cookies='[]', ai_targets=['好友'])
                hit = {'conv_id':'a','display':'好友','title':'好友','is_group':False}
                im = MagicMock(ready=True, last_scan={'scanned_all':True})
                im.wait_ready.return_value = {'status':'READY'}
                im.iter_conversations.return_value = [hit]
                im.read_chat_messages.side_effect = [[], rows, rows]
                provider = MagicMock()
                provider.reply.return_value = '收到啦'
                def send(*args, **kwargs):
                    stop.set()
                    return {'ok':True}
                im.type_and_send.side_effect = send
                with patch('cloakbrowser.launch'), patch('core.ai.runner.DouyinIM', return_value=im), \
                     patch('core.ai.runner.create_provider', return_value=provider):
                    run_account(account, config.ai_chat, config, stop, lambda *_:None)
                im.type_and_send.assert_called_once_with(hit, '收到啦', log_content=False)
                self.assertEqual(provider.reply.call_args.args[0][-1]['content'], rows[0]['text'])
                self.assertEqual(im.read_chat_messages.call_count, 3)
                for call in im.read_chat_messages.call_args_list:
                    self.assertEqual(call.kwargs, {'include_media':True})
