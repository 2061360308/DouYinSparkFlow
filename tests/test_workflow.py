import unittest
from pathlib import Path


class ScheduleWorkflowTests(unittest.TestCase):
    def test_schedule_uses_the_user_data_environment_and_uppercase_secret(self):
        workflow = Path(".github/workflows/schedule.yml").read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn('cron: "0 1 * * *"', workflow)
        self.assertIn("environment: user-data", workflow)
        self.assertIn("WZ_DATA: ${{ secrets.WZ_DATA }}", workflow)
        self.assertIn("python main.py --doTask", workflow)

    def test_wz_web_chat_probe_is_manual_and_uses_only_wz_data(self):
        workflow = Path(".github/workflows/wz_web_chat_test.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("runs-on: ubuntu-22.04", workflow)
        self.assertIn("environment: user-data", workflow)
        self.assertIn("WZ_DATA: ${{ secrets.WZ_DATA }}", workflow)
        self.assertNotIn("GSY_DATA", workflow)
        self.assertIn("python web_chat_probe.py", workflow)


if __name__ == "__main__":
    unittest.main()
