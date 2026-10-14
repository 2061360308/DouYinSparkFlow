"""Cookie parsing and task exit status; no accounts, browser or network."""
import json
import unittest
from unittest.mock import patch

from utils import config as config_module
from core import tasks, fc_server


class CookieConfigTests(unittest.TestCase):
    def load(self, raw):
        task = {"unique_id": "example", "username": "测试账号", "targets": ["好友"]}
        with patch.dict("os.environ", {"TASKS": json.dumps([task]), "COOKIES_EXAMPLE": raw}, clear=True), \
             patch.object(config_module, "userData", None):
            return config_module.get_userData()

    def test_cookie_values_survive_json_decoding(self):
        for value in ["普通ASCII", "中文火花", "line\nnext\tvalue", r"C:\new\x", 'quote"and\\slash']:
            with self.subTest(value=value):
                cookies = [{"name": "sessionid", "value": value, "sameSite": "Lax"}]
                for ensure_ascii in (True, False):
                    result = self.load(json.dumps(cookies, ensure_ascii=ensure_ascii))
                    self.assertEqual(result[0]["cookies"][0]["value"], value)
                    self.assertNotIn("sameSite", result[0]["cookies"][0])
                    self.assertEqual(result[0]["targets"], ["好友"])

    def test_invalid_json_is_skipped_without_logging_cookie_value(self):
        secret = "fake-cookie-secret"
        with patch.object(config_module.logger, "warning") as warning:
            self.assertEqual(self.load(secret), [])
        self.assertNotIn(secret, str(warning.call_args_list))

    def test_non_cookie_arrays_are_skipped(self):
        for data in ({"name": "sessionid"}, ["cookie"], None, 42):
            with self.subTest(data=data):
                self.assertEqual(self.load(json.dumps(data)), [])

    def test_one_bad_account_does_not_discard_valid_account(self):
        with patch.dict("os.environ", {
            "TASKS": json.dumps([{"unique_id": "bad"}, {"unique_id": "good"}]),
            "COOKIES_BAD": "invalid", "COOKIES_GOOD": '[{"name":"sessionid","value":"fake"}]',
        }, clear=True), patch.object(config_module, "userData", None):
            self.assertEqual([u["unique_id"] for u in config_module.get_userData()], ["good"])


class TaskExitStatusTests(unittest.TestCase):
    def test_no_runnable_accounts_is_failure_without_starting_browser(self):
        with patch.object(tasks, "userData", []), patch.object(tasks, "get_browser") as browser:
            self.assertEqual(tasks.runTasks(), 1)
        browser.assert_not_called()

    def test_failed_account_exit_is_propagated_by_function_server(self):
        with patch.object(tasks, "runTasks", return_value=1), \
             patch.object(fc_server, "_apply_event_env", return_value=[]):
            status, result = fc_server.run_once()
        self.assertEqual(status, 500)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)

    def test_success_response_is_unchanged(self):
        with patch.object(tasks, "runTasks", return_value=0), \
             patch.object(fc_server, "_apply_event_env", return_value=["TASKS"]):
            self.assertEqual(fc_server.run_once(), (200, {"ok": True, "env_keys": ["TASKS"]}))

    def test_failure_releases_function_server_lock(self):
        with patch.object(tasks, "runTasks", side_effect=[RuntimeError("fake error"), 0]), \
             patch.object(fc_server, "_apply_event_env", return_value=[]):
            self.assertEqual(fc_server.run_once()[0], 500)
            self.assertEqual(fc_server.run_once()[0], 200)
