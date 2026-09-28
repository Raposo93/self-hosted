import fcntl
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "http-endpoint-monitor.sh"


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        monitor_dir = self.root / "http-endpoint-monitor"
        notifier_dir = self.root / "mail-notifier"
        commands_dir = self.root / "bin"
        monitor_dir.mkdir()
        notifier_dir.mkdir()
        commands_dir.mkdir()
        self.script = monitor_dir / "http-endpoint-monitor.sh"
        shutil.copy2(SCRIPT, self.script)
        self.attempts = self.root / "mail-attempts"
        self.http_calls = self.root / "http-calls"
        self.state = monitor_dir / "state" / "service.state"
        self.alert = monitor_dir / "state" / "service.alert"
        self.lock = monitor_dir / "state" / "service.lock"
        self._script(
            notifier_dir / "send-mail.sh",
            '#!/usr/bin/env bash\ncat >> "$TEST_MAIL_ATTEMPTS"\nprintf "\\n---\\n" >> "$TEST_MAIL_ATTEMPTS"\nexit "$TEST_MAIL_STATUS"\n',
        )
        self._script(
            commands_dir / "curl",
            '#!/usr/bin/env bash\nprintf "call\\n" >> "$TEST_HTTP_CALLS"\nprintf "%s|0.1" "$TEST_HTTP_CODE"\nexit "$TEST_CURL_STATUS"\n',
        )
        self._script(commands_dir / "logger", "#!/usr/bin/env bash\nexit 0\n")
        self._script(
            commands_dir / "date",
            '#!/usr/bin/env bash\nif [[ "$1" == "+%s" ]]; then printf "%s\\n" "$TEST_NOW"; else printf "test-time\\n"; fi\n',
        )
        self.env = {
            **os.environ,
            "PATH": f"{commands_dir}:{os.environ['PATH']}",
            "TEST_MAIL_ATTEMPTS": str(self.attempts),
            "TEST_HTTP_CALLS": str(self.http_calls),
            "TEST_MAIL_STATUS": "0",
            "TEST_HTTP_CODE": "200",
            "TEST_CURL_STATUS": "0",
            "TEST_NOW": "1000",
        }

    def _script(self, path, content):
        path.write_text(content)
        path.chmod(0o755)

    def run_monitor(self, *, http="200", mail=0, now=1000):
        env = {
            **self.env,
            "TEST_HTTP_CODE": http,
            "TEST_MAIL_STATUS": str(mail),
            "TEST_NOW": str(now),
        }
        return subprocess.run(
            [
                "bash",
                str(self.script),
                "--name",
                "service",
                "--url",
                "https://example.com",
                "--account",
                "test",
                "--from",
                "sender@example.com",
                "--email-alert",
                "recipient@example.com",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def attempt_count(self):
        return (
            self.attempts.read_text().count("\n---\n") if self.attempts.exists() else 0
        )

    def test_failed_down_alert_retries_after_interval_and_stops_on_success(self):
        self.assertEqual(self.run_monitor(http="503").returncode, 0)
        self.assertEqual(self.run_monitor(http="503").returncode, 0)
        self.assertEqual(self.run_monitor(http="503", mail=1).returncode, 1)
        self.assertEqual(self.state.read_text(), "DOWN|3\n")
        self.assertEqual(self.alert.read_text(), "UNKNOWN|DOWN|1000\n")
        self.assertEqual(self.run_monitor(http="503", mail=1, now=1100).returncode, 0)
        self.assertEqual(self.attempt_count(), 1)
        self.assertEqual(self.state.read_text(), "DOWN|4\n")
        self.assertEqual(self.run_monitor(http="503", now=1300).returncode, 0)
        self.assertEqual(self.attempt_count(), 2)
        self.assertEqual(self.alert.read_text(), "DOWN|NONE|0\n")
        self.assertEqual(self.run_monitor(http="503", now=1700).returncode, 0)
        self.assertEqual(self.attempt_count(), 2)
        log = (self.script.parent / "http-endpoint-monitor.log").read_text()
        self.assertIn("Email alert pending: DOWN", log)
        self.assertIn("ERROR email alert delivery failed: DOWN", log)
        self.assertIn("Email alert accepted by notifier: DOWN", log)

    def test_failed_recovery_retries_and_clears_on_success(self):
        for _ in range(3):
            self.run_monitor(http="503")
        self.assertEqual(self.alert.read_text(), "DOWN|NONE|0\n")
        self.assertEqual(self.run_monitor(mail=1, now=1000).returncode, 1)
        self.assertEqual(self.state.read_text(), "UP|0\n")
        self.assertEqual(self.alert.read_text(), "DOWN|UP|1000\n")
        self.assertEqual(self.run_monitor(mail=1, now=1100).returncode, 0)
        self.assertEqual(self.run_monitor(now=1300).returncode, 0)
        self.assertEqual(self.alert.read_text(), "UP|NONE|0\n")
        self.assertEqual(self.attempt_count(), 3)
        self.assertEqual(self.run_monitor(now=1700).returncode, 0)
        self.assertEqual(self.attempt_count(), 3)

    def test_recovery_discards_unsent_down_alert(self):
        for _ in range(3):
            self.run_monitor(http="503", mail=1)
        self.assertEqual(self.run_monitor(now=1100).returncode, 0)
        self.assertEqual(self.state.read_text(), "UP|0\n")
        self.assertEqual(self.alert.read_text(), "UNKNOWN|NONE|0\n")
        self.assertEqual(self.attempt_count(), 1)
        self.run_monitor(now=1400)
        self.assertEqual(self.attempt_count(), 1)

    def test_unsent_recovery_is_cancelled_on_new_downtime(self):
        for _ in range(3):
            self.run_monitor(http="503")
        self.run_monitor(mail=1)
        for _ in range(3):
            self.run_monitor(http="503")
        self.assertEqual(self.alert.read_text(), "DOWN|NONE|0\n")
        self.assertEqual(self.attempt_count(), 2)

    def test_lock_prevents_simultaneous_checks(self):
        self.run_monitor()
        with self.lock.open("w") as lock_stream:
            fcntl.flock(lock_stream, fcntl.LOCK_EX)
            self.assertEqual(self.run_monitor(http="503").returncode, 0)
        self.assertEqual(self.http_calls.read_text().count("call\n"), 1)


if __name__ == "__main__":
    unittest.main()
