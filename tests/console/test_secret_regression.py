import subprocess
import unittest
from pathlib import Path


class SecretRegressionTests(unittest.TestCase):
    def test_tracked_files_contain_no_runtime_secret_files(self):
        tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
        forbidden = {"usersData.json", "spark.db", "cookie.key", "session.key", ".env.console"}
        self.assertTrue(forbidden.isdisjoint(Path(path).name for path in tracked))


if __name__ == "__main__":
    unittest.main()
