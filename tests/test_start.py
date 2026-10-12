import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell = os.environ.get("TATOTALK_TEST_SHELL") or shutil.which("sh")
        if not cls.shell:
            raise unittest.SkipTest("A POSIX shell is needed for startup validation tests")
        cls.start = Path(__file__).resolve().parents[1] / "scripts" / "start.sh"

    def invoke(self, password):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            upstream = app / "mixins" / "scripts" / "run.sh"
            upstream.parent.mkdir(parents=True)
            upstream.write_text("#!/bin/sh\nprintf 'UPSTREAM_STARTED\\n'\nexit 7\n", encoding="utf-8")
            upstream.chmod(0o755)
            env = os.environ.copy()
            env.update({"APP_PATH": app.as_posix(), "VNC_PASSWORD": password})
            return subprocess.run([self.shell, self.start.as_posix()], env=env, capture_output=True, text=True, timeout=10)

    def test_valid_password_executes_upstream_and_preserves_exit_code(self):
        result = self.invoke("Abc12345")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout.strip(), "UPSTREAM_STARTED")

    def test_missing_or_wrong_length_password_stops_before_upstream(self):
        for password in ("", "1234567", "123456789"):
            with self.subTest(password=password):
                result = self.invoke(password)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_shell_metacharacters_are_rejected(self):
        for password in ("abc$;123", "ab c1234", "abc`1234"):
            with self.subTest(password=password):
                result = self.invoke(password)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_non_ascii_password_is_rejected(self):
        result = self.invoke("abcdefg中")
        self.assertEqual(result.returncode, 64, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
