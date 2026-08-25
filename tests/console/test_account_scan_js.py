import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "console" / "account_scan_js_harness.js"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for account scan UI tests")
class AccountScanJavaScriptTests(unittest.TestCase):
    def run_scenario(self, scenario: str):
        completed = subprocess.run(
            [shutil.which("node"), str(HARNESS), scenario],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(f'"scenario":"{scenario}"', completed.stdout)

    def test_close_during_pending_start_cancels_returned_scan_before_hiding(self):
        self.run_scenario("pending-close")

    def test_cancel_during_pending_start_cancels_returned_scan_before_hiding(self):
        self.run_scenario("pending-cancel")

    def test_cancel_failure_stays_visible_shows_fixed_error_and_allows_retry(self):
        self.run_scenario("active-cancel-failure")

    def test_preloaded_scan_is_reused_when_binding_button_is_clicked(self):
        self.run_scenario("preload-click")

    def test_leaving_page_sends_background_cancellation(self):
        self.run_scenario("pagehide-cancel")

    def test_success_message_closes_dialog_before_refresh(self):
        self.run_scenario("success-close")


if __name__ == "__main__":
    unittest.main()
