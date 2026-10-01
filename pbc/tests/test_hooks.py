"""Check hook ordering, failure propagation, and notification without PBS."""

import os
import subprocess
import tempfile
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


if __name__ == "__main__":
    unittest.main()
