import os
from pathlib import Path
import subprocess
import sys
import unittest
from scripts.release_tag import resolve_tag


class ReleaseTagTests(unittest.TestCase):
    def test_push_uses_tag_ref(self):
        self.assertEqual(resolve_tag("push", "v3.3.1", "v9.9.9", "3.0.0"), "v3.3.1")

    def test_manual_input_and_version_fallback(self):
        self.assertEqual(resolve_tag("workflow_dispatch", "main", "release-v3.3.2", "3.3.1"), "release-v3.3.2")
        self.assertEqual(resolve_tag("workflow_dispatch", "main", "", "3.3.1\n"), "v3.3.1")

    def test_existing_tag_formats_and_prerelease_suffixes(self):
        for tag in ["3.0.1", "v3.3.1", "release-v3.3.2", "v3.4.0-rc.1+build.2"]:
            self.assertEqual(resolve_tag("push", tag, "", ""), tag)

    def test_shell_and_output_syntax_is_rejected_as_data(self):
        for tag in ['v1.0;$(echo test)', 'v1.0";echo test;"', 'v1`echo test`',
                    'v1\ntag=other', 'v1\rtest', '-v1', '', 'v1..2', 'v1.lock', 'v1.']:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                resolve_tag("push", tag, "", "3.3.1")

    def test_invalid_manual_input_cannot_fall_back_to_version(self):
        with self.assertRaises(ValueError):
            resolve_tag("workflow_dispatch", "main", "v1\nextra=true", "3.3.1")

    def test_cli_emits_single_metadata_line_or_no_output(self):
        script = Path(__file__).resolve().parents[1] / "scripts/release_tag.py"
        for tag, code, output in [("v3.3.1", 0, "tag=v3.3.1\n"), ("v1\nother=true", 1, "")]:
            result = subprocess.run([sys.executable, str(script)], cwd=script.parent.parent,
                env={**os.environ, "GITHUB_EVENT_NAME": "workflow_dispatch", "INPUT_TAG": tag,
                     "PYTHONIOENCODING": "utf-8"}, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, code)
            self.assertEqual(result.stdout, output)
