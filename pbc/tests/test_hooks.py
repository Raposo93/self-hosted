"""Check hook ordering, failure propagation, and notification without PBS."""

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "pbc_backup_data.sh"


class HookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        binaries = self.base / "bin"
        binaries.mkdir()
        for name, body in {
            "proxmox-backup-client": 'printf "backup\\n" >> "$EVENTS"\nexit "${BACKUP_STATUS:-0}"\n',
            "curl": 'printf "health\\n" >> "$EVENTS"\n',
            "msmtp": 'cat > "$MAIL"\n',
        }.items():
            path = binaries / name
            path.write_text("#!/usr/bin/env bash\n" + body)
            path.chmod(0o755)
        for phase in ("pre", "post"):
            path = self.base / f"{phase} hook.sh"
            path.write_text(
                "#!/usr/bin/env bash\n"
                f'printf "{phase}\\n" >> "$EVENTS"\n'
                f'exit "${{{phase.upper()}_STATUS:-0}}"\n'
            )
            path.chmod(0o755)
        source = self.base / "source"
        source.mkdir()
        self.events = self.base / "events"
        self.log = self.base / "backup.log"
        self.mail = self.base / "mail"
        self.environment = {
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "EVENTS": str(self.events),
            "MAIL": str(self.mail),
            "LOGFILE": str(self.log),
            "SOURCE_DIR": str(source),
            "REPO": "fake-repository",
            "BACKUP_NAME": "data.pxar",
            "RECIPIENT_EMAIL": "to@example.com",
            "SENDER_EMAIL": "from@example.com",
            "MSMTP_ACCOUNT": "default",
            "PRE_BACKUP_HOOK": str(self.base / "pre hook.sh"),
            "POST_BACKUP_HOOK": str(self.base / "post hook.sh"),
        }

    def run_backup(self, **settings):
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=self.environment | settings,
            capture_output=True,
            text=True,
            check=False,
        )

    def run_cancelled_backup(self, *, post_status="0", repeat_signal=False):
        client = self.base / "bin" / "proxmox-backup-client"
        client.write_text(
            "#!/usr/bin/env bash\n"
            'trap \'sleep 0.2; printf "client-exit\\n" >> "$EVENTS"; exit 143\' TERM\n'
            'printf "backup\\n" >> "$EVENTS"\n'
            "while :; do sleep 0.05; done\n"
        )
        process = subprocess.Popen(
            ["bash", str(SCRIPT)],
            env=self.environment | {"POST_STATUS": post_status},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if (
                    self.events.exists()
                    and "backup" in self.events.read_text().splitlines()
                ):
                    break
                if process.poll() is not None:
                    self.fail("Backup exited before cancellation")
                time.sleep(0.01)
            else:
                self.fail("Backup did not start")
            os.killpg(process.pid, signal.SIGTERM)
            if repeat_signal:
                os.kill(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            return process.returncode, stdout, stderr
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def test_successful_hooks_surround_backup(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.events.read_text().splitlines(), ["pre", "backup", "post"]
        )
        self.assertIn("Pre-backup hook exit code: 0", self.log.read_text())
        self.assertIn("Post-backup hook exit code: 0", self.log.read_text())

    def test_failed_pre_hook_skips_backup_and_post_hook(self):
        result = self.run_backup(
            PRE_STATUS="12", HEALTHCHECK_URL="https://example.test/id"
        )
        self.assertEqual(result.returncode, 12)
        self.assertEqual(self.events.read_text().splitlines(), ["pre"])
        self.assertIn("Backup not started", self.log.read_text())
        self.assertIn("Pre-backup hook exit code: 12", self.mail.read_text())
        self.assertIn("Backup failed", self.mail.read_text())

    def test_post_hook_runs_after_failed_backup(self):
        result = self.run_backup(BACKUP_STATUS="23")
        self.assertEqual(result.returncode, 23)
        self.assertEqual(
            self.events.read_text().splitlines(), ["pre", "backup", "post"]
        )
        self.assertIn("Backup exit code: 23", self.mail.read_text())

    def test_both_failures_are_logged_and_backup_status_is_returned(self):
        result = self.run_backup(BACKUP_STATUS="23", POST_STATUS="17")
        self.assertEqual(result.returncode, 23)
        self.assertIn("Backup exit code: 23", self.mail.read_text())
        self.assertIn("Post-backup hook exit code: 17", self.mail.read_text())

    def test_post_hook_failure_fails_successful_backup_run(self):
        result = self.run_backup(POST_STATUS="17")
        self.assertEqual(result.returncode, 17)
        self.assertEqual(
            self.events.read_text().splitlines(), ["pre", "backup", "post"]
        )
        self.assertIn("Backup failed", self.mail.read_text())

    def test_nonexecutable_post_hook_fails_before_preparation(self):
        result = self.run_backup(POST_BACKUP_HOOK=str(self.base / "missing.sh"))
        self.assertEqual(result.returncode, 126)
        self.assertFalse(self.events.exists())
        self.assertIn(
            "Post-backup hook is not an executable file", self.mail.read_text()
        )

    def test_sigterm_waits_for_client_then_runs_post_hook_once(self):
        status, _, stderr = self.run_cancelled_backup(repeat_signal=True)
        self.assertEqual(status, 143, stderr)
        self.assertEqual(
            self.events.read_text().splitlines(),
            ["pre", "backup", "client-exit", "post"],
        )
        self.assertIn("Backup cancelled by SIGTERM", self.log.read_text())
        self.assertIn("Backup failed", self.mail.read_text())
        self.assertNotIn("Backup completed", self.mail.read_text())

    def test_post_hook_failure_after_sigterm_remains_failure(self):
        status, _, stderr = self.run_cancelled_backup(post_status="17")
        self.assertEqual(status, 143, stderr)
        self.assertEqual(self.events.read_text().splitlines()[-1], "post")
        self.assertIn("Post-backup hook exit code: 17", self.mail.read_text())
        self.assertIn("Backup failed", self.mail.read_text())


if __name__ == "__main__":
    unittest.main()
