"""scheduler 模块单元测试。

全部注入假 runner / 临时目录，不碰真实系统（不调用 schtasks / crontab）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SCHEDULER_BACKEND", "noop")
os.environ.setdefault("APP_SCHEDULE_AUTOREGISTER", "0")

from app import paths
from app.config import env_store
from app.scheduler import api, core, state
from app.scheduler.backends import LinuxBackend, NoopBackend, WindowsBackend
from app.scheduler.core import Launcher, LockBusy, RunLock


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class PathsTestCase(unittest.TestCase):
    """把 scheduler.paths 的所有落盘位置指到临时目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = {
            key: getattr(paths, key)
            for key in (
                "SCHEDULER_DIR",
                "SCHEDULER_STATE",
                "SCHEDULER_INSTALL",
                "SCHEDULER_HISTORY",
                "SCHEDULER_LOCK",
                "SCHEDULER_LOG",
                "ENV_FILE",
            )
        }
        paths.SCHEDULER_DIR = self.root / ".scheduler"
        paths.SCHEDULER_STATE = paths.SCHEDULER_DIR / "state.json"
        paths.SCHEDULER_INSTALL = paths.SCHEDULER_DIR / "install.json"
        paths.SCHEDULER_HISTORY = paths.SCHEDULER_DIR / "history.json"
        paths.SCHEDULER_LOCK = paths.SCHEDULER_DIR / "lock"
        paths.SCHEDULER_LOG = self.root / "logs" / "scheduler.log"
        paths.ENV_FILE = self.root / ".env"

    def tearDown(self):
        for key, value in self._saved.items():
            setattr(paths, key, value)
        self.tmp.cleanup()


class ConfigTests(unittest.TestCase):
    def test_resolve_run_time_from_env(self):
        env = {"CRON_HOUR": "7", "CRON_MINUTE": "5", "CRON_SECOND": "30"}
        self.assertEqual(core.resolve_run_time(env), "07:05")

    def test_resolve_run_time_defaults_and_clamps(self):
        self.assertEqual(core.resolve_run_time({}), "09:00")
        self.assertEqual(core.resolve_run_time({"CRON_HOUR": "99", "CRON_MINUTE": "-3"}), "23:00")

    def test_read_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('CRON_HOUR=8\n# comment\nCRON_MINUTE="30"\n\n', encoding="utf-8")
            data = env_store.read_env_map(path)
            self.assertEqual(data["CRON_HOUR"], "8")
            self.assertEqual(data["CRON_MINUTE"], "30")


class LauncherTests(unittest.TestCase):
    def test_source_argv(self):
        launcher = Launcher(exe="python", frozen=False, root=Path("C:/repo"))
        self.assertEqual(launcher.task_argv(), ["python", str(Path("C:/repo") / "main.py"), "task"])
        self.assertEqual(
            launcher.scheduler_argv("run-if-due"),
            ["python", str(Path("C:/repo") / "main.py"), "scheduler", "run-if-due"],
        )

    def test_frozen_argv(self):
        launcher = Launcher(exe="app.exe", frozen=True, root=Path("C:/app"))
        self.assertEqual(launcher.task_argv(), ["app.exe", "task"])
        self.assertEqual(launcher.scheduler_argv("run-if-due"), ["app.exe", "scheduler", "run-if-due"])

    def test_for_exe_and_python(self):
        exe = Launcher.for_exe("C:/dist/app.exe")
        self.assertTrue(exe.frozen)
        self.assertEqual(exe.task_argv(), [str(Path("C:/dist/app.exe").resolve()), "task"])


class StateTests(PathsTestCase):
    def test_success_roundtrip(self):
        self.assertFalse(state.succeeded_today("2026-10-03"))
        state.record_success("2026-10-03")
        self.assertTrue(state.succeeded_today("2026-10-03"))
        self.assertFalse(state.succeeded_today("2026-10-04"))

    def test_attempt_does_not_mark_success(self):
        state.record_attempt(1, "2026-10-03")
        self.assertFalse(state.succeeded_today("2026-10-03"))
        self.assertEqual(state.load_state()["last_exit_code"], 1)

    def test_history_records_attempt_and_success(self):
        state.record_attempt(1, "2026-10-03")
        day = state.history_days()["2026-10-03"]
        self.assertEqual(day["attempts"], 1)
        self.assertFalse(day["success"])
        self.assertEqual(day["last_exit_code"], 1)

        state.record_success("2026-10-03")
        day = state.history_days()["2026-10-03"]
        self.assertTrue(day["success"])
        self.assertEqual(day["last_exit_code"], 0)


class LockTests(PathsTestCase):
    def test_acquire_and_release(self):
        lock_path = self.root / "lock"
        with RunLock(lock_path):
            self.assertTrue(lock_path.exists())
        self.assertFalse(lock_path.exists())

    def test_busy(self):
        lock_path = self.root / "lock"
        with RunLock(lock_path):
            with self.assertRaises(LockBusy):
                with RunLock(lock_path):
                    pass

    def test_stale_lock_reclaimed(self):
        lock_path = self.root / "lock"
        lock_path.write_text("999", encoding="utf-8")
        old = os.path.getmtime(lock_path) - (RunLock.STALE_SECONDS + 10)
        os.utime(lock_path, (old, old))
        with RunLock(lock_path):
            pass
        self.assertFalse(lock_path.exists())


class WindowsBackendTests(PathsTestCase):
    def setUp(self):
        super().setUp()
        self._appdata_backup = os.environ.get("APPDATA")
        os.environ["APPDATA"] = str(self.root)

    def tearDown(self):
        if self._appdata_backup is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._appdata_backup
        super().tearDown()

    def _launcher(self):
        return Launcher(exe="python", frozen=False, root=self.root)

    def test_build_scheduled_command(self):
        backend = WindowsBackend()
        args = backend.build_scheduled_command(name="X", run_time="09:30", vbs=r"C:\r\run_task.vbs")
        self.assertEqual(args[:5], ["schtasks", "/Create", "/F", "/TN", "X"])
        self.assertIn("DAILY", args)
        self.assertIn("09:30", args)
        self.assertTrue(any("wscript.exe" in a for a in args))

    def test_scheduled_install_writes_vbs_and_registers(self):
        calls = []

        def runner(args):
            calls.append(args)
            return FakeResult(0)

        backend = WindowsBackend(runner=runner)
        backend.install(name="X", mode="scheduled", run_time="09:00", launcher=self._launcher())
        self.assertTrue(backend.task_vbs().is_file())
        self.assertEqual(calls[0][0], "schtasks")
        self.assertTrue(any("wscript.exe" in a for a in calls[0]))
        backend.uninstall(name="X")
        self.assertEqual(calls[-1][:4], ["schtasks", "/Delete", "/TN", "X"])

    def test_boot_install_writes_startup_vbs(self):
        backend = WindowsBackend(runner=lambda args: FakeResult(0))
        backend.install(name="X", mode="boot", run_time="09:00", launcher=self._launcher())
        vbs = backend.boot_vbs("X")
        self.assertTrue(vbs.is_file())
        text = vbs.read_text(encoding="utf-8")
        self.assertIn("WScript.Shell", text)
        self.assertIn("run-if-due", text)
        self.assertIn(", 0, False", text)
        self.assertTrue(backend.status(name="X", mode="boot")["installed"])
        backend.uninstall(name="X")
        self.assertFalse(vbs.is_file())


class LinuxBackendTests(PathsTestCase):
    def _launcher(self):
        return Launcher(exe="python", frozen=False, root=self.root)

    def test_build_line(self):
        backend = LinuxBackend()
        line = backend.build_line(name="X", mode="scheduled", run_time="09:05", wrapper="/r/.scheduler/run_task.sh")
        self.assertTrue(line.startswith("5 9 * * * "))
        self.assertIn("# X", line)
        boot = backend.build_line(name="X", mode="boot", run_time="09:05", wrapper="/r/run_if_due.sh")
        self.assertTrue(boot.startswith("@reboot "))

    def test_install_uninstall_idempotent(self):
        store = {"text": "0 1 * * * /usr/bin/other\n"}

        def runner(args, input_text=None):
            if args[0] == "crontab" and args[1] == "-l":
                return FakeResult(0, stdout=store["text"])
            if args[0] == "crontab" and args[1] == "-":
                store["text"] = input_text
                return FakeResult(0)
            return FakeResult(1)

        backend = LinuxBackend(runner=runner)
        backend.install(name="X", mode="scheduled", run_time="09:00", launcher=self._launcher())
        backend.install(name="X", mode="scheduled", run_time="10:00", launcher=self._launcher())
        lines = [line for line in store["text"].splitlines() if "# X" in line]
        self.assertEqual(len(lines), 1)  # 幂等：不重复追加
        self.assertIn("0 10 * * *", lines[0])
        self.assertIn("/usr/bin/other", store["text"])  # 别人的行不受影响

        self.assertTrue(backend.uninstall(name="X"))
        self.assertNotIn("# X", store["text"])


class CoreTests(PathsTestCase):
    def test_write_vbs(self):
        launcher = Launcher(exe="python", frozen=False, root=self.root)
        path = self.root / "run_task.vbs"
        core.write_vbs(launcher, path, launcher.task_argv())
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("WScript.Shell", text)
        self.assertIn(str(self.root), text)
        self.assertIn("main.py", text)
        self.assertIn(", 0, False", text)
        if paths.browser_binary().is_file():
            self.assertIn("CLOAKBROWSER_BINARY_PATH", text)

    def test_write_sh(self):
        launcher = Launcher(exe="python", frozen=False, root=self.root)
        path = self.root / "run_task.sh"
        core.write_sh(launcher, path, launcher.task_argv())
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("#!/bin/sh", text)
        self.assertIn(str(self.root), text)
        self.assertIn("main.py", text)
        if paths.browser_binary().is_file():
            self.assertIn("CLOAKBROWSER_BINARY_PATH", text)

    def test_run_if_due_skips_when_succeeded_today(self):
        from datetime import date

        state.record_success(date.today().isoformat())
        with mock.patch("app.scheduler.core.run_task") as run:
            result = core.run_if_due()
        run.assert_not_called()
        self.assertFalse(result["ran"])

    def test_run_if_due_runs_and_marks_success(self):
        with mock.patch("app.scheduler.core.run_task", return_value=0) as run:
            result = core.run_if_due()
        run.assert_called_once()
        self.assertTrue(result["ran"])
        self.assertEqual(result["exit_code"], 0)
        from datetime import date

        self.assertTrue(state.succeeded_today(date.today().isoformat()))

    def test_run_if_due_failure_not_marked_success(self):
        with mock.patch("app.scheduler.core.run_task", return_value=1):
            result = core.run_if_due()
        self.assertTrue(result["ran"])
        self.assertEqual(result["exit_code"], 1)
        from datetime import date

        self.assertFalse(state.succeeded_today(date.today().isoformat()))

    def test_run_if_due_force(self):
        from datetime import date

        state.record_success(date.today().isoformat())
        with mock.patch("app.scheduler.core.run_task", return_value=0) as run:
            result = core.run_if_due(force=True)
        run.assert_called_once()
        self.assertTrue(result["ran"])


class ApiTests(PathsTestCase):
    def _launcher(self):
        return Launcher(exe="python", frozen=False, root=self.root)

    def test_set_mode_scheduled(self):
        backend = NoopBackend()
        with mock.patch("app.scheduler.api.get_backend", return_value=backend):
            status = api.set_mode(api.MODE_SCHEDULED, run_time="08:30", launcher=self._launcher(), backend=backend)
        self.assertEqual(status["mode"], api.MODE_SCHEDULED)
        self.assertEqual(status["run_time"], "08:30")
        self.assertTrue(status["installed"])
        self.assertEqual(state.load_install()["mode"], api.MODE_SCHEDULED)

    def test_set_mode_boot(self):
        backend = NoopBackend()
        with mock.patch("app.scheduler.api.get_backend", return_value=backend):
            status = api.set_mode(api.MODE_BOOT, launcher=self._launcher(), backend=backend)
        self.assertEqual(status["mode"], api.MODE_BOOT)
        self.assertTrue(status["installed"])

    def test_set_mode_config_uninstalls(self):
        backend = NoopBackend()
        with mock.patch("app.scheduler.api.get_backend", return_value=backend):
            api.set_mode(api.MODE_SCHEDULED, launcher=self._launcher(), backend=backend)
            status = api.set_mode(api.MODE_CONFIG, launcher=self._launcher(), backend=backend)
        self.assertEqual(status["mode"], api.MODE_CONFIG)
        self.assertFalse(status["installed"])

    def test_set_mode_normalizes_seconds(self):
        backend = NoopBackend()
        with mock.patch("app.scheduler.api.get_backend", return_value=backend):
            status = api.set_mode(
                api.MODE_SCHEDULED, run_time="09:55:00", launcher=self._launcher(), backend=backend
            )
        self.assertEqual(status["run_time"], "09:55")

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            api.set_mode("nope", launcher=self._launcher(), backend=NoopBackend())

    def test_uninstall(self):
        backend = NoopBackend()
        with mock.patch("app.scheduler.api.get_backend", return_value=backend):
            api.set_mode(api.MODE_SCHEDULED, launcher=self._launcher(), backend=backend)
            status = api.uninstall(backend=backend)
        self.assertEqual(status["mode"], "")

    def test_cancel_keeps_mode_but_uninstalls(self):
        backend = NoopBackend()
        with mock.patch("app.scheduler.api.get_backend", return_value=backend):
            api.set_mode(api.MODE_SCHEDULED, launcher=self._launcher(), backend=backend)
            status = api.cancel(backend=backend)
        self.assertEqual(status["mode"], api.MODE_SCHEDULED)  # 模式保留
        self.assertFalse(status["installed"])                 # 已移除注册

    def test_ensure_default_mode_disabled_by_env(self):
        with mock.patch.dict(os.environ, {"APP_SCHEDULE_AUTOREGISTER": "0"}):
            api.ensure_default_mode()
        self.assertFalse(state.load_install().get("mode"))

    def test_ensure_default_mode_registers_scheduled(self):
        backend = NoopBackend()
        with mock.patch.dict(os.environ, {"APP_SCHEDULE_AUTOREGISTER": "1"}), \
             mock.patch("app.scheduler.api.get_backend", return_value=backend), \
             mock.patch("app.scheduler.api.Launcher.detect", return_value=self._launcher()):
            status = api.ensure_default_mode()
        self.assertEqual(status["mode"], api.MODE_SCHEDULED)


if __name__ == "__main__":
    unittest.main()
