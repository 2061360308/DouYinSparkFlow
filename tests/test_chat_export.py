"""History collection and durable exports, with synthetic identities only."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from core.chat_export import collect_history, write_export
from app.browser.sessions import AccountOperator, BrowserSession
from tests.test_web_account_ops import FakeWorker
from app.web.bridge import Bridge
from app.browser.worker import BrowserLoginWorker
from core.douyin_im import STATUS_READY


def message(n):
    return dict(id=str(n), text=f'文字 {n}\n第二行', created_at=1700000000+n,
                from_me=n % 2 == 0, sender_id='synthetic', order=str(n))


class FakeIM:
    def __init__(self, windows):
        self.windows = windows
        self.index = 0
        self.page = self
        self.conv = 'test-conversation'

    def _current_conv(self):
        return {'convId': self.conv}

    def read_chat_messages(self, _):
        return [message(n) for n in self.windows[self.index]]

    def evaluate(self, _, direction):
        if direction == 'older':
            self.index = min(self.index + 1, len(self.windows) - 1)
        return dict(top=(len(self.windows)-1-self.index)*100, height=1000, viewport=200)

    def wait_for_timeout(self, _):
        pass


class ExportTests(unittest.TestCase):
    def test_history_can_exceed_fifty_messages(self):
        im = FakeIM([list(range(101, 151)), list(range(61, 111)),
                     list(range(21, 71)), list(range(1, 31))])
        history = collect_history(im, im.conv, stop=threading.Event())
        self.assertEqual(len(history['messages']), 150)
        self.assertEqual(history['messages'][0]['id'], '1')
        self.assertEqual(history['messages'][-1]['id'], '150')

    def test_virtual_windows_are_deduplicated_and_sorted(self):
        im = FakeIM([[5, 6], [3, 4, 5], [1, 2, 3]])
        history = collect_history(im, im.conv, stop=threading.Event())
        self.assertEqual([m['id'] for m in history['messages']], list('123456'))
        self.assertEqual(history['stop_reason'], 'no_more_loaded')
        self.assertFalse(history['complete'])

    def test_cancel_preserves_collected_messages(self):
        stop = threading.Event()
        im = FakeIM([[5, 6], [3, 4]])
        history = collect_history(im, im.conv, stop=stop, progress=lambda _: stop.set())
        self.assertEqual([m['id'] for m in history['messages']], ['5', '6'])
        self.assertEqual(history['stop_reason'], 'cancelled')

    def test_switching_conversations_cannot_mix_history(self):
        im = FakeIM([[5, 6], [3, 4]])
        def switch(_):
            im.conv = 'another-conversation'
        history = collect_history(im, 'test-conversation', stop=threading.Event(), progress=switch)
        self.assertEqual([m['id'] for m in history['messages']], ['5', '6'])
        self.assertEqual(history['stop_reason'], 'conversation_changed')

    def test_timeout_is_distinct_from_reaching_top(self):
        im = FakeIM([[1]])
        ticks = iter([0, 0, 901, 901])
        history = collect_history(im, im.conv, stop=threading.Event(), clock=lambda: next(ticks))
        self.assertEqual(history['stop_reason'], 'timeout')
        self.assertEqual(len(history['messages']), 1)

    def test_pair_is_readable_scoped_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            history = dict(messages=[message(1), message(2)], complete=False,
                           stop_reason='cancelled', elapsed_seconds=2)
            kwargs = dict(account='test', friend='../好友:CON', conv_id='123', history=history)
            first = write_export(root, **kwargs)
            second = write_export(root, **kwargs)
            self.assertNotEqual(first['folder'], second['folder'])
            self.assertTrue(Path(first['txt_path']).is_relative_to(Path(root)))
            data = json.loads(Path(first['json_path']).read_text(encoding='utf-8'))
            self.assertEqual(data['messages'], history['messages'])
            self.assertEqual(data['metadata']['message_count'], 2)
            self.assertTrue(data['metadata']['first_message_at'].endswith('+08:00'))
            text = Path(first['txt_path']).read_text(encoding='utf-8-sig')
            self.assertIn('未确认全部历史', text)
            self.assertIn('文字 1\n第二行', text)
            self.assertIn('我', text)

    def test_export_session_dispatch_and_result_delivery(self):
        bridge = Bridge()
        ops = AccountOperator(bridge)
        worker = FakeWorker('synthetic')
        session = BrowserSession(session_id='test', mode='export', worker=worker,
                                 profile_dir='synthetic', folder='synthetic', fingerprint='',
                                 existing_unique_id='synthetic')
        session.export_payload = {'friend': 'synthetic'}
        ops.sessions['test'] = session
        ops._handle(session, 'opened', {})
        self.assertEqual(worker.sent[-1], ('chat_export', session.export_payload))
        result = {'metadata': {'message_count': 2}}
        ops._handle(session, 'chat_exported', result)
        self.assertEqual(worker.sent[-1][0], 'shutdown')
        self.assertTrue(any(p['kind'] == 'chat_exported' for _, p in bridge.drain()))
        ops._handle(session, 'done', None)
        self.assertNotIn('test', ops.sessions)

    def test_worker_exports_selected_friend_to_both_files(self):
        im = FakeIM([[3, 4], [1, 2, 3]])
        im.wait_ready = lambda: {'status': STATUS_READY, 'user_id': 'synthetic-uid'}
        im.iter_conversations = lambda: iter([{'conv_id': im.conv, 'display': '测试好友', 'is_group': False}])
        im.last_scan = {'scanned_all': True}
        im.select_conversation = lambda cid: cid == im.conv
        im.detach = Mock()
        worker = BrowserLoginWorker(profile_dir='synthetic')
        worker.page = im.page
        worker.is_running = lambda: True
        with tempfile.TemporaryDirectory() as root, \
             patch('app.browser.worker.douyin_im.DouyinIM', return_value=im), \
             patch('app.browser.worker.paths.APP_DIR', Path(root)):
            worker._chat_export({'unique_id': 'synthetic', 'uid': 'synthetic-uid', 'friend': '测试好友'})
            events = list(worker.events.queue)
            result = next(data for kind, data in events if kind == 'chat_exported')
            self.assertEqual(result['metadata']['message_count'], 4)
            self.assertTrue(Path(result['txt_path']).is_file())
            self.assertTrue(Path(result['json_path']).is_file())
        im.detach.assert_called_once()

    def test_worker_refuses_wrong_account_before_scanning_messages(self):
        im = Mock()
        im.wait_ready.return_value = {'status': STATUS_READY, 'user_id': 'another-uid'}
        worker = BrowserLoginWorker(profile_dir='synthetic')
        worker.is_running = lambda: True
        with patch('app.browser.worker.douyin_im.DouyinIM', return_value=im):
            with self.assertRaisesRegex(ValueError, '当前登录账号'):
                worker._chat_export({'unique_id': 'synthetic', 'uid': 'synthetic-uid', 'friend': '好友'})
        im.iter_conversations.assert_not_called()
        im.detach.assert_called_once()

    def test_stop_command_interrupts_busy_worker_without_waiting_for_queue(self):
        worker = BrowserLoginWorker(profile_dir='synthetic')
        worker.send('export_stop')
        self.assertTrue(worker.export_stop.is_set())
        self.assertTrue(worker.commands.empty())


if __name__ == '__main__':
    unittest.main()
