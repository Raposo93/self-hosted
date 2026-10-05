"""Exercise backup profile identity without contacting PBS or sending mail."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "pbc_backup_data.py"


class BackupIdTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        binaries = self.base / "bin"
        binaries.mkdir()
        client = binaries / "proxmox-backup-client"
        client.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['CLIENT_CALLS'], 'a') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        )
        client.chmod(0o755)
        mail = binaries / "msmtp"
        mail.write_text("#!/usr/bin/env bash\ncat >/dev/null\n")
        mail.chmod(0o755)
        source = self.base / "source"
        source.mkdir()
        self.calls = self.base / "calls.jsonl"
        self.environment = {
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "CLIENT_CALLS": str(self.calls),
            "LOGFILE": str(self.base / "backup.log"),
            "SOURCE_DIR": str(source),
            "REPO": "fake-repository",
            "BACKUP_NAME": "data.pxar",
            "RECIPIENT_EMAIL": "to@example.com",
            "SENDER_EMAIL": "from@example.com",
            "MSMTP_ACCOUNT": "default",
        }

    def run_backup(self, backup_id=None, namespace=None, **settings):
        environment = self.environment.copy()
        if backup_id is not None:
            environment["BACKUP_ID"] = backup_id
        if namespace is not None:
            environment["NAMESPACE"] = namespace
        environment.update(settings)
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_distinct_profiles_pass_distinct_ids_as_single_arguments(self):
        for backup_id in ("photos", "documents"):
            with self.subTest(backup_id=backup_id):
                self.assertEqual(self.run_backup(backup_id).returncode, 0)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        for call, backup_id in zip(calls, ("photos", "documents"), strict=True):
            self.assertEqual(
                call[:2], ["backup", "data.pxar:" + self.environment["SOURCE_DIR"]]
            )
            self.assertEqual(call[call.index("--backup-id") + 1], backup_id)

    def test_omitted_id_preserves_client_default(self):
        self.assertEqual(self.run_backup().returncode, 0)
        self.assertNotIn("--backup-id", json.loads(self.calls.read_text()))

    def test_empty_and_invalid_ids_fail_before_client(self):
        for backup_id in ("", "bad/id", "-bad", "two words", "a\nb"):
            with self.subTest(backup_id=backup_id):
                result = self.run_backup(backup_id)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BACKUP_ID", result.stderr)
                self.assertFalse(self.calls.exists())

    def test_namespace_is_optional_and_passed_as_one_argument(self):
        self.assertEqual(self.run_backup().returncode, 0)
        self.assertEqual(self.run_backup(namespace="").returncode, 0)
        self.assertEqual(self.run_backup(namespace="team backups").returncode, 0)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertNotIn("--ns", calls[0])
        self.assertNotIn("--ns", calls[1])
        self.assertEqual(calls[2][calls[2].index("--ns") + 1], "team backups")

    def test_encryption_key_and_systemd_credential_are_preserved(self):
        credentials = self.base / "credentials"
        credentials.mkdir()
        (credentials / "proxmox-backup-client.encryption-password").write_text(
            "fake credential"
        )
        keyfile = self.base / "key.json"
        keyfile.write_text("fake key")
        result = self.run_backup(
            CREDENTIALS_DIRECTORY=str(credentials), ENCRYPTION_KEYFILE=str(keyfile)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(self.calls.read_text())
        self.assertEqual(call[call.index("--keyfile") + 1], str(keyfile))

        self.calls.unlink()
        result = self.run_backup(ENCRYPTION_KEYFILE=str(keyfile))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Incomplete encryption configuration", result.stderr)
        self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
