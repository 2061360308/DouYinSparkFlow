"""Atomic configuration saves, including real Windows sharing violations."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

from app.config import env_store
from app.config.models import Config


class AtomicSaveTests(unittest.TestCase):
    def test_full_save_replaces_once_and_preserves_custom_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('# custom comment\nCUSTOM="keep this"\nTASKS=[]\nTASKS=[]', encoding='utf-8')
            original_replace = os.replace
            with patch.object(env_store.os, 'replace', wraps=original_replace) as replace:
                env_store.save_config(Config(), path)
            self.assertEqual(replace.call_count, 1)
            text = path.read_text(encoding='utf-8')
            self.assertIn('# custom comment\nCUSTOM="keep this"\n', text)
            self.assertEqual(text.count('TASKS='), 1)
            self.assertEqual(dotenv_values(path)['CUSTOM'], 'keep this')
            self.assertFalse(list(Path(tmp).glob('.tmp_*')))
            self.assertEqual(env_store.unset_keys(['CUSTOM', 'missing'], path), 1)
            self.assertNotIn('CUSTOM', dotenv_values(path))

    @unittest.skipUnless(os.name == 'nt', 'Windows retry behavior')
    def test_temporary_access_denied_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('CUSTOM=old\n', encoding='utf-8')
            original_replace = os.replace
            error = PermissionError('temporary lock')
            error.winerror = 5
            calls = 0
            def replace(source, target):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise error
                original_replace(source, target)
            with patch.object(env_store.os, 'replace', side_effect=replace):
                env_store._write_updates(path, {'CUSTOM':'new'})
            self.assertEqual(calls, 3)
            self.assertEqual(dotenv_values(path)['CUSTOM'], 'new')

    def test_failure_keeps_original_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            original = b'# original\nCUSTOM=keep\n'
            path.write_bytes(original)
            with patch.object(env_store.os, 'replace', side_effect=PermissionError('locked')):
                with self.assertRaises(PermissionError):
                    env_store._write_updates(path, {'CUSTOM':'changed'})
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(list(Path(tmp).glob('.tmp_*')))

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing')
    def test_real_windows_file_lock_releases_before_retry(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('CUSTOM=old\n', encoding='utf-8')
            handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            # Allow reads but deny replacement until the reader closes.
            timer = threading.Timer(0.2, lambda: kernel.CloseHandle(handle))
            timer.start()
            try:
                env_store._write_updates(path, {'CUSTOM':'new'})
                self.assertEqual(dotenv_values(path)['CUSTOM'], 'new')
            finally:
                timer.join()
