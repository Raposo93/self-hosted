"""Check mount safeguards without accessing real mounts or PBS."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "pbc_backup_data.py"


class MountTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        binaries = self.base / "bin"
        binaries.mkdir()
        for name, body in {
            "proxmox-backup-client": (
                "import json, os, sys\n"
                "with open(os.environ['CLIENT_CALLS'], 'a') as output:\n"
                "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            ),
            "findmnt": (
                "import json, os, sys\n"
                "mounts = json.loads(os.environ['FAKE_MOUNTS'])\n"
                "args = sys.argv[1:]\n"
                "path = args[args.index('--target') + 1] if '--target' in args "
                "else args[args.index('--mountpoint') + 1]\n"
                "matches = [(point, source) for point, source in mounts.items() "
                "if path == point or ('--target' in args and path.startswith(point.rstrip('/') + '/'))]\n"
                "if not matches: sys.exit(1)\n"
                "point, source = max(matches, key=lambda item: len(item[0]))\n"
                "print(point if args[-1] == 'TARGET' else source)\n"
            ),
        }.items():
            path = binaries / name
            path.write_text("#!/usr/bin/env python3\n" + body)
            path.chmod(0o755)
        mail = binaries / "msmtp"
        mail.write_text("#!/usr/bin/env bash\ncat >/dev/null\n")
        mail.chmod(0o755)
        self.mount = self.base / "disk"
        self.source = self.mount / "data"
        self.source.mkdir(parents=True)
        self.calls = self.base / "calls.jsonl"
        self.environment = {
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "CLIENT_CALLS": str(self.calls),
            "LOGFILE": str(self.base / "backup.log"),
            "SOURCE_DIR": str(self.source),
            "REPO": "fake-repository",
            "BACKUP_NAME": "data.pxar",
            "RECIPIENT_EMAIL": "to@example.com",
            "SENDER_EMAIL": "from@example.com",
            "MSMTP_ACCOUNT": "default",
            "FAKE_MOUNTS": json.dumps({str(self.base): "root"}),
        }

    def run_backup(self, **settings):
        environment = self.environment | settings
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_rejected(self, result, reason):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(reason, result.stderr)
        self.assertFalse(self.calls.exists())
        if (self.base / "backup.log").exists():
            self.assertNotIn("Backup completed", (self.base / "backup.log").read_text())

    def test_local_directory_needs_no_mount(self):
        self.assertEqual(self.run_backup().returncode, 0)

    def test_source_argument_keeps_configured_symlink(self):
        alias = self.base / "source alias"
        alias.symlink_to(self.source, target_is_directory=True)
        result = self.run_backup(SOURCE_DIR=str(alias))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.calls.read_text())[1], f"data.pxar:{alias}")

    def test_residual_directory_does_not_count_as_mount(self):
        self.assert_rejected(
            self.run_backup(EXPECTED_MOUNT=str(self.mount)), "Expected mount"
        )

    def test_nested_source_on_expected_mount(self):
        mounts = {str(self.base): "root", str(self.mount): "server:/data"}
        result = self.run_backup(
            EXPECTED_MOUNT=str(self.mount),
            EXPECTED_MOUNT_SOURCE="server:/data",
            FAKE_MOUNTS=json.dumps(mounts),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrong_mount_source(self):
        mounts = {str(self.base): "root", str(self.mount): "other:/data"}
        self.assert_rejected(
            self.run_backup(
                EXPECTED_MOUNT=str(self.mount),
                EXPECTED_MOUNT_SOURCE="server:/data",
                FAKE_MOUNTS=json.dumps(mounts),
            ),
            "Unexpected mount source",
        )

    def test_include_dev_passes_separate_arguments(self):
        first = self.source / "photos with spaces"
        second = self.source / "media"
        first.mkdir()
        second.mkdir()
        mounts = {
            str(self.base): "root",
            str(self.mount): "server:/data",
            str(first): "disk1",
            str(second): "disk2",
        }
        result = self.run_backup(
            EXPECTED_MOUNT=str(self.mount),
            INCLUDE_DEV_MOUNTS=f"{first}|{second}",
            FAKE_MOUNTS=json.dumps(mounts),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.calls.read_text())
        self.assertEqual(
            [args[i + 1] for i, arg in enumerate(args) if arg == "--include-dev"],
            [str(first), str(second)],
        )

    def test_unmounted_include_is_rejected(self):
        nested = self.source / "empty-mount"
        nested.mkdir()
        self.assert_rejected(
            self.run_backup(INCLUDE_DEV_MOUNTS=str(nested)), "not mounted"
        )

    def test_root_source_accepts_mounted_include(self):
        mounts = {"/": "root", str(self.mount): "disk"}
        result = self.run_backup(
            SOURCE_DIR="/",
            INCLUDE_DEV_MOUNTS=str(self.mount),
            FAKE_MOUNTS=json.dumps(mounts),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.calls.read_text())
        self.assertEqual(
            [args[i + 1] for i, arg in enumerate(args) if arg == "--include-dev"],
            [str(self.mount)],
        )

    def test_include_outside_nonroot_source_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.assert_rejected(
            self.run_backup(INCLUDE_DEV_MOUNTS=str(outside)),
            "outside SOURCE_DIR",
        )


if __name__ == "__main__":
    unittest.main()
