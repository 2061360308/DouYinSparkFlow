import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CoreTasksImportTests(unittest.TestCase):
    def test_message_confirmation_helper_import_needs_no_legacy_config_files(self):
        project_root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(project_root)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from core.tasks import confirm_message_sent; print(confirm_message_sent.__name__)",
                ],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("confirm_message_sent", result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
