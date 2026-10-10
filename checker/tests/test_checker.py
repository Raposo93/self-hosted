import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

CHECKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHECKER / "checks"))


def module(name):
    spec = importlib.util.spec_from_file_location(
        name, CHECKER / "checks" / f"{name}.py"
    )
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


notification = module("notification")
sys.modules["notification"] = notification
disk = module("disk-space")
security = module("security-updates")


class DiskTests(unittest.TestCase):
    def test_mount_escapes_virtual_and_bind_duplicates(self):
        table = (
            "1 0 8:1 / / rw - ext4 /dev/sda rw\n"
            "2 0 8:1 /data /alias rw - ext4 /dev/sda rw\n"
            "3 0 0:1 / /proc rw - proc proc rw\n"
            "4 0 8:2 / /with\\040space rw - xfs /dev/sdb rw\n"
        )
        self.assertEqual(
            disk.mounts(table),
            [("8:1", "/", "/dev/sda"), ("8:2", "/with space", "/dev/sdb")],
        )

    def test_statvfs_reserved_space_and_inodes(self):
        stats = SimpleNamespace(
            f_blocks=100, f_bfree=20, f_bavail=10, f_frsize=4096, f_files=100, f_ffree=5
        )
        with patch.object(disk.os, "statvfs", return_value=stats):
            values = disk.usage("/fixture")
            self.assertEqual(values["capacity"], 89)
            self.assertEqual(values["inodes"], 95)
            self.assertEqual(values["available_bytes"], 40960)
            stats.f_files = 0
            self.assertEqual(disk.usage("/fixture")["inodes"], 0)

    def test_thresholds_inode_alert_and_worsening(self):
        self.assertEqual(
            disk.status({"capacity": 20, "inodes": 95}, 85, 95), "CRITICAL"
        )
        self.assertEqual(disk.status({"capacity": 85, "inodes": 20}, 85, 95), "WARNING")
        old = {"status": "WARNING", "capacity": 85, "inodes": 10}
        self.assertFalse(disk.changed(old, old))
        self.assertTrue(disk.changed(old, {**old, "capacity": 86}))
        self.assertTrue(disk.changed(old, {**old, "status": "CRITICAL"}))

    def test_real_du_depth_and_direct_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large" / "nested" / "last").mkdir(parents=True)
            (root / "small").mkdir()
            (root / "large" / "nested" / "last" / "payload").write_bytes(b"x" * 65536)
            lines, complete = disk.diagnose(directory, 1024**4, 3, 10)
            self.assertTrue(complete)
            self.assertEqual(sum(line.startswith("Level") for line in lines), 3)
            self.assertIn("last", lines[-1])
            self.assertTrue(any("differ materially" in line for line in lines))
            lines, complete = disk.diagnose(str(root / "small"), 0, 3, 10)
            self.assertTrue(complete)
            self.assertIn("No nonempty child", lines[-1])

    def test_du_timeout_and_permission_error_are_incomplete(self):
        for effect, result in (
            (subprocess.TimeoutExpired("du", 1), None),
            (None, subprocess.CompletedProcess("du", 1)),
        ):
            with patch.object(
                disk.subprocess, "run", side_effect=effect, return_value=result
            ):
                lines, complete = disk.diagnose("/fixture", 0, 3, 1)
                self.assertFalse(complete)
                self.assertIn("incomplete", lines[-1])

    def test_notifications_suppress_retry_worsen_and_rearm(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ, {"CHECKER_STATE_DIR": directory, "HOST_NAME": "fixture"}
            ),
            patch.object(
                disk, "mounts", return_value=[("8:1", "/fixture", "/dev/fixture")]
            ),
            patch.object(disk, "usage") as usage,
            patch.object(
                disk, "diagnose", return_value=(["diagnosis"], True)
            ) as diagnose,
            patch.object(disk, "send") as send,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            values = {
                "capacity": 85,
                "inodes": 1,
                "used_bytes": 100,
                "available_bytes": 15,
                "inode_free": 99,
                "inode_total": 100,
            }
            usage.return_value = values
            send.side_effect = subprocess.CalledProcessError(1, "mail")
            self.assertEqual(disk.main(), 1)
            send.side_effect = None
            self.assertEqual(disk.main(), 0)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(disk.main(), 0)
            self.assertEqual(send.call_count, 2)
            usage.return_value = {**values, "capacity": 86}
            self.assertEqual(disk.main(), 0)
            self.assertEqual(send.call_count, 3)
            usage.return_value = {**values, "capacity": 50}
            self.assertEqual(disk.main(), 0)
            self.assertEqual(send.call_count, 3)
            usage.return_value = values
            self.assertEqual(disk.main(), 0)
            self.assertEqual(send.call_count, 4)
            diagnose.return_value = (["incomplete"], False)
            usage.return_value = {**values, "capacity": 96}
            self.assertEqual(disk.main(), 1)
            self.assertEqual(disk.main(), 1)
            self.assertEqual(send.call_count, 6)


class SecurityAndMailTests(unittest.TestCase):
    def test_security_changes_recovery_and_failed_delivery(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"CHECKER_STATE_DIR": directory}),
            patch.object(security, "send") as send,
        ):
            security.notify("SECURITY UPDATES", "packages", "host")
            security.notify("SECURITY UPDATES", "packages", "host")
            self.assertEqual(send.call_count, 1)
            security.notify("SECURITY UPDATES", "new packages", "host")
            security.notify("OK", "ok", "host")
            security.notify("SECURITY UPDATES", "new packages", "host")
            self.assertEqual(send.call_count, 3)
            send.side_effect = subprocess.CalledProcessError(1, "mail")
            with self.assertRaises(subprocess.CalledProcessError):
                security.notify("REBOOT REQUIRED", "kernel", "host")
            self.assertEqual(
                notification.load("security-updates"), {"body": "new packages"}
            )

    def test_engine_routes_attention_and_ok_to_callback(self):
        modules = {
            "apt": SimpleNamespace(Cache=dict),
            "apt_pkg": SimpleNamespace(
                Cache=lambda _: SimpleNamespace(packages=[]),
                SELSTATE_HOLD=2,
                version_compare=lambda a, b: 0,
            ),
        }
        with (
            patch.object(sys, "argv", ["check", "--no-refresh"]),
            patch.dict(
                os.environ,
                {"RECIPIENT_EMAIL": "test@example.com", "HOST_NAME": "fixture"},
            ),
            patch.object(
                security.engine.importlib,
                "import_module",
                side_effect=modules.__getitem__,
            ),
            patch.object(
                security.engine, "pending_updates", return_value=[]
            ) as updates,
            patch.object(security.engine, "reboot_reasons", return_value=[]),
            patch.object(security.engine, "send_notification") as legacy,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            calls = []
            self.assertEqual(
                security.engine.main(notification=lambda *args: calls.append(args)), 0
            )
            self.assertEqual(calls[-1][0], "OK")
            updates.return_value = ["openssl"]
            self.assertEqual(
                security.engine.main(notification=lambda *args: calls.append(args)), 0
            )
            self.assertEqual(calls[-1][0], "SECURITY UPDATES")
            self.assertEqual(calls[-1][2], "fixture")
            legacy.assert_not_called()

    def test_corrupt_state_fails_without_mail(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"CHECKER_STATE_DIR": directory}),
            patch.object(disk, "send") as send,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            (Path(directory) / "disk-space.json").write_text('{"fixture": "bad"}')
            self.assertEqual(disk.main(), 1)
            send.assert_not_called()

    def test_shared_notifier_interface(self):
        with (
            patch.dict(
                os.environ,
                {
                    "HOST_NAME": "fixture",
                    "SEND_MAIL": "/fixture/send-mail.sh",
                    "RECIPIENT_EMAIL": "test@example.com",
                },
            ),
            patch.object(notification.subprocess, "run") as run,
        ):
            notification.send("WARNING", "body")
            self.assertEqual(
                run.call_args.args[0][:5],
                [
                    "/fixture/send-mail.sh",
                    "--to",
                    "test@example.com",
                    "--subject",
                    "[fixture] WARNING",
                ],
            )
            self.assertEqual(run.call_args.kwargs["input"], "body")


class RunnerTests(unittest.TestCase):
    def test_isolation_selection_disabled_validation_and_lock(self):
        import fcntl

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "checks").mkdir()
            (root / "run.sh").write_text((CHECKER / "run.sh").read_text())
            (root / "checks/config.py").write_text(
                (CHECKER / "checks/config.py").read_text()
            )
            (root / "checks/security-updates.py").write_text(
                "print('SECURITY FAILED')\nraise SystemExit(1)\n"
            )
            (root / "checks/disk-space.py").write_text("print('DISK RAN')\n")
            notifier = root / "notifier"
            notifier.write_text("#!/bin/sh\nexit 0\n")
            notifier.chmod(0o700)
            configuration = f"HOST_NAME=fixture\nRECIPIENT_EMAIL=test@example.com\nSEND_MAIL={notifier}\nCHECKER_STATE_DIR={root}/state\nCHECK_SECURITY_UPDATES=true\nCHECK_DISK_SPACE=true\n"
            (root / ".env").write_text(configuration)

            def run(*args):
                return subprocess.run(
                    ["bash", str(root / "run.sh"), *args],
                    capture_output=True,
                    text=True,
                    check=False,
                    env={"PATH": os.environ["PATH"]},
                )

            result = run()
            self.assertEqual(result.returncode, 1)
            self.assertIn("DISK RAN", result.stdout)
            result = run("disk-space")
            self.assertEqual(result.returncode, 0)
            self.assertNotIn("SECURITY FAILED", result.stdout)
            with (root / "state/run.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIn("Another checker run", run().stderr)
            (root / ".env").write_text(
                configuration.replace(
                    "CHECK_SECURITY_UPDATES=true", "CHECK_SECURITY_UPDATES=false"
                )
            )
            self.assertEqual(run().returncode, 0)
            (root / ".env").write_text(
                configuration + "DISK_WARNING=99\nDISK_CRITICAL=95\n"
            )
            result = run()
            self.assertNotIn("DISK RAN", result.stdout)
            self.assertEqual(result.returncode, 1)
