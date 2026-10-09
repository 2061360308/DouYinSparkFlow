"""Delivery uncertainty regression tests; no browser, account or network needed."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.douyin_im import DouyinIM, STATUS_READY
from core import tasks


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.im = object.__new__(DouyinIM)
        self.im.page = Mock()
        self.im.page.evaluate.return_value = True
        self.im.mon = SimpleNamespace(sends=[])
        self.im._input_mode = Mock(return_value="real")
        self.im._editor = Mock(return_value=Mock())
        self.im._msg_state = Mock(return_value={"count": 0})
        self.im._click_send = Mock(return_value="button")
        self.im._wait_receipt = Mock(return_value={"ok": False, "http": None, "dom": None})
        self.hit = {"conv_id": "fake-conversation", "display": "测试好友"}

    def test_receipt_timeout_is_uncertain_and_cannot_retry(self):
        result = self.im.type_and_send(self.hit, "测试消息")
        self.assertEqual(result["delivery_state"], "uncertain")
        self.assertFalse(result["ok"])
        self.assertFalse(result["safe_to_retry"])
        self.im._click_send.assert_called_once()

    def test_missing_editor_is_not_sent_and_can_retry(self):
        self.im._editor.return_value = None
        result = self.im.type_and_send(self.hit, "测试消息")
        self.assertEqual(result["delivery_state"], "not_sent")
        self.assertTrue(result["safe_to_retry"])
        self.im._click_send.assert_not_called()

    def test_error_during_send_is_uncertain(self):
        self.im._click_send.side_effect = RuntimeError("page disconnected after click")
        result = self.im.type_and_send(self.hit, "测试消息")
        self.assertEqual(result["delivery_state"], "uncertain")
        self.assertFalse(result["safe_to_retry"])

    def test_error_waiting_for_receipt_is_uncertain(self):
        self.im._wait_receipt.side_effect = RuntimeError("page closed")
        self.assertEqual(self.im.type_and_send(self.hit, "测试消息")["delivery_state"], "uncertain")

    def test_dom_confirmation_is_success(self):
        self.im._wait_receipt.return_value = {
            "ok": True, "http": None, "dom": {"count": 1},
        }
        result = self.im.type_and_send(self.hit, "测试消息")
        self.assertEqual(result["delivery_state"], "confirmed")
        self.assertTrue(result["ok"])
        self.assertFalse(result["safe_to_retry"])

    def test_http_confirmation_is_success(self):
        self.im._wait_receipt.return_value = {
            "ok": True, "dom": None,
            "http": {"ok": True, "code": 0, "status": "ok", "message_id": "fake-message"},
        }
        self.assertEqual(self.im.type_and_send(self.hit, "测试消息")["delivery_state"], "confirmed")

    def test_disabled_receipt_wait_is_not_confirmation(self):
        result = self.im.type_and_send(self.hit, "测试消息", wait_receipt=False)
        self.assertEqual(result["delivery_state"], "uncertain")
        self.assertFalse(result["ok"])
        self.im._wait_receipt.assert_not_called()

    def test_send_boundary_resets_between_calls(self):
        self.im.type_and_send(self.hit, "第一条")
        self.im._editor.return_value = None
        result = self.im.type_and_send(self.hit, "第二条")
        self.assertEqual(result["delivery_state"], "not_sent")
        self.assertTrue(result["safe_to_retry"])

    def test_click_exception_does_not_fall_back_to_enter(self):
        im = object.__new__(DouyinIM)
        im.page = Mock()
        button = im.page.locator.return_value.first
        button.count.return_value = 1
        button.is_visible.return_value = True
        button.click.side_effect = RuntimeError("click acknowledgement lost")
        with self.assertRaises(RuntimeError):
            im._click_send()
        im.page.keyboard.press.assert_not_called()

    def test_absent_button_can_use_enter(self):
        im = object.__new__(DouyinIM)
        im.page = Mock()
        im.page.locator.return_value.first.count.return_value = 0
        self.assertEqual(im._click_send(), "enter")
        im.page.keyboard.press.assert_called_once_with("Enter")


class TaskDeliveryTests(unittest.TestCase):
    def run_task(self, responses, count=1):
        browser = Mock()
        im = Mock()
        im.wait_ready.return_value = {"status": STATUS_READY}
        self.reselect = Mock(return_value=True)
        friends = [
            {"display": f"好友{i}", "conv_id": str(i), "reselect": self.reselect}
            for i in range(count)
        ]
        im.iter_find_and_select.return_value = iter(friends)
        im.type_and_send.side_effect = responses
        im.last_scan = {}
        im.fold_groups.return_value = {}
        with patch.object(tasks, "DouyinIM", return_value=im), patch.object(tasks, "build_message", return_value="测试消息"):
            result = tasks.do_user_task(browser, "测试账号", [], [])
        return result, im

    def test_uncertain_is_not_resent_and_next_friend_continues(self):
        result, im = self.run_task([
            {"ok": False, "delivery_state": "uncertain", "safe_to_retry": False},
            {"ok": True, "delivery_state": "confirmed", "safe_to_retry": False},
        ], count=2)
        self.assertEqual(im.type_and_send.call_count, 2)
        self.reselect.assert_not_called()
        self.assertEqual(result["sent_ok"], 1)
        self.assertEqual(result["sent_fail"], 0)
        self.assertEqual(result["sent_uncertain"], 1)
        self.assertEqual(result["uncertain"], ["好友0"])

    def test_pre_send_failure_can_retry_once(self):
        result, im = self.run_task([
            {"ok": False, "delivery_state": "not_sent", "safe_to_retry": True},
            {"ok": True, "delivery_state": "confirmed", "safe_to_retry": False},
        ])
        self.assertEqual(im.type_and_send.call_count, 2)
        self.reselect.assert_called_once()
        self.assertEqual(result["sent_ok"], 1)
        self.assertEqual(result["sent_fail"], 0)

    def test_retry_becoming_uncertain_is_not_retried_again(self):
        result, im = self.run_task([
            {"ok": False, "delivery_state": "not_sent", "safe_to_retry": True},
            {"ok": False, "delivery_state": "uncertain", "safe_to_retry": False},
        ])
        self.assertEqual(im.type_and_send.call_count, 2)
        self.assertEqual(result["sent_uncertain"], 1)

    def test_pre_send_retries_are_bounded(self):
        result, im = self.run_task([
            {"ok": False, "delivery_state": "not_sent", "safe_to_retry": True},
            {"ok": False, "delivery_state": "not_sent", "safe_to_retry": True},
        ])
        self.assertEqual(im.type_and_send.call_count, 2)
        self.assertEqual(result["sent_fail"], 1)

    def test_failure_without_explicit_retry_permission_is_not_resent(self):
        result, im = self.run_task([{"ok": False}])
        im.type_and_send.assert_called_once()
        self.reselect.assert_not_called()
        self.assertEqual(result["sent_fail"], 1)

    def test_notification_contains_uncertain_count_and_recipient(self):
        result = {"ok": True, "sent_ok": 4, "sent_fail": 1, "sent_uncertain": 1, "uncertain": ["测试好友"]}
        with patch.dict(tasks.config, {"notifications": [{"type": "test"}]}), patch("core.notify.send_all", return_value=[]) as send:
            tasks._notify_summary([("测试账号", result)], 0)
        message = send.call_args.args[1]
        self.assertIn("待确认 1", message)
        self.assertIn("测试好友", message)
        self.assertIn("未自动重发", message)


if __name__ == "__main__":
    unittest.main()
