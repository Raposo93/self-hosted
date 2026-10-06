import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "pbc_restore_check", Path(__file__).resolve().parents[1] / "pbc_restore_check.py"
)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class RestoreTests(unittest.TestCase):
    def test_same_archive_and_sentinel_restore_from_each_profile_group(self):
        rows = [
            {"backup-type": "host", "backup-id": "photos", "backup-time": 100},
            {"backup-type": "host", "backup-id": "documents", "backup-time": 200},
        ]
        for backup_id, timestamp in (("photos", "00:01:40"), ("documents", "00:03:20")):
            with (
                self.subTest(backup_id=backup_id),
                tempfile.TemporaryDirectory() as base,
            ):
                calls = []

                def fake_client(arguments, calls=calls):
                    calls.append(arguments)
                    if arguments[0] == "snapshot":
                        return json.dumps(rows)
                    target = Path(arguments[3])
                    target.mkdir()
                    (target / ".pbc-restore-sentinel").write_text(
                        "pbc-restore-sentinel-v1\n"
                    )
                    return ""

                with (
                    patch.dict(
                        os.environ,
                        {
                            "REPO": "fake",
                            "RESTORE_GROUP": f"host/{backup_id}",
                            "BACKUP_NAME": "data.pxar",
                            "RESTORE_TMP_BASE": base,
                        },
                        clear=True,
                    ),
                    patch.object(checker, "client", side_effect=fake_client),
                    patch.object(checker.time, "time", return_value=300),
                ):
                    self.assertIn(f"host/{backup_id}", checker.verify())
                self.assertEqual(calls[0][2], f"host/{backup_id}")
                self.assertEqual(
                    calls[1][1], f"host/{backup_id}/1970-01-01T{timestamp}Z"
                )
                self.assertEqual(calls[1][2], "data.pxar")

    def test_latest_filters_group_and_sorts(self):
        rows = [
            {"backup-type": "host", "backup-id": "test", "backup-time": 100},
            {"backup-type": "host", "backup-id": "other", "backup-time": 400},
            {"backup-type": "host", "backup-id": "test", "backup-time": 200},
        ]
        self.assertEqual(
            checker.latest_snapshot(json.dumps(rows), "host/test", 0, 300),
            "host/test/1970-01-01T00:03:20Z",
        )
        with self.assertRaises(ValueError):
            checker.latest_snapshot(json.dumps(rows), "host/test", 50, 300)
        with self.assertRaises(ValueError):
            checker.latest_snapshot("[]", "host/test", 0, 300)
        with self.assertRaises(ValueError):
            checker.latest_snapshot(json.dumps(rows), "host/test", 0, -200)

    def exercise(self, mode):
        with tempfile.TemporaryDirectory() as base:
            environment = {
                "REPO": "fake",
                "RESTORE_GROUP": "host/test",
                "BACKUP_NAME": "data.pxar",
                "RESTORE_TMP_BASE": base,
            }
            calls = []

            def fake_client(arguments):
                calls.append(arguments)
                if arguments[0] == "snapshot":
                    return json.dumps(
                        [
                            {
                                "backup-type": "host",
                                "backup-id": "test",
                                "backup-time": 100,
                            }
                        ]
                    )
                target = Path(arguments[3])
                target.mkdir()
                sentinel = target / ".pbc-restore-sentinel"
                if mode == "restore-error":
                    raise ValueError("Restore failed")
                if mode == "interrupted":
                    raise InterruptedError("Interrupted")
                if mode == "symlink":
                    sentinel.symlink_to("/etc/passwd")
                elif mode != "missing":
                    sentinel.write_text(
                        "bad\n" if mode == "bad" else "pbc-restore-sentinel-v1\n"
                    )
                return ""

            if mode == "checksum":
                environment["SENTINEL_SHA256"] = "0" * 64
            with (
                patch.dict(os.environ, environment, clear=True),
                patch.object(checker, "client", side_effect=fake_client),
            ):
                if mode == "ok":
                    self.assertIn("host/test", checker.verify())
                else:
                    with self.assertRaises((ValueError, InterruptedError)):
                        checker.verify()
            self.assertEqual(list(Path(base).iterdir()), [])
            self.assertIn("/.pbc-restore-sentinel", calls[1])

    def test_restore_and_cleanup(self):
        for mode in [
            "ok",
            "missing",
            "bad",
            "checksum",
            "symlink",
            "restore-error",
            "interrupted",
        ]:
            with self.subTest(mode=mode):
                self.exercise(mode)

    def test_client_failure_and_timeout(self):
        with (
            patch.dict(os.environ, {"PBS_NAMESPACE": "wrong-space"}),
            patch.object(checker.subprocess, "run") as run,
        ):
            run.return_value.returncode = 5
            run.return_value.stderr = "private token"
            with self.assertRaisesRegex(ValueError, "exit 5"):
                checker.client(["restore"])
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertNotIn("PBS_NAMESPACE", run.call_args.kwargs["env"])
            run.side_effect = subprocess.TimeoutExpired("client", 1)
            with self.assertRaises(subprocess.TimeoutExpired):
                checker.client(["restore"])

    def test_namespace_selection_and_no_root_fallback(self):
        for name, configured, inherited, expected in [
            ("omitted", None, None, ""),
            ("omitted-with-client-default", None, "photos", ""),
            ("empty", "", "photos", ""),
            ("configured", "photos", None, "photos"),
            ("conflict", "documents", "photos", "documents"),
        ]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as base:
                environment = {
                    "REPO": "fake",
                    "RESTORE_GROUP": "host/same",
                    "BACKUP_NAME": "data.pxar",
                    "RESTORE_TMP_BASE": base,
                    "PBS_PASSWORD": "fake password",
                    "PBS_FINGERPRINT": "fake fingerprint",
                    "CREDENTIALS_DIRECTORY": base,
                }
                if configured is not None:
                    environment["NAMESPACE"] = configured
                if inherited is not None:
                    environment["PBS_NAMESPACE"] = inherited
                calls = []

                def fake_run(
                    arguments,
                    calls=calls,
                    expected=expected,
                    environment=environment,
                    **kwargs,
                ):
                    received = kwargs["env"]
                    self.assertNotIn("PBS_NAMESPACE", received)
                    for key, value in environment.items():
                        if key != "PBS_NAMESPACE":
                            self.assertEqual(received[key], value)
                    arguments = arguments[1:]
                    calls.append(arguments)
                    selected = (
                        arguments[arguments.index("--ns") + 1]
                        if "--ns" in arguments
                        else ""
                    )
                    self.assertEqual(selected, expected)
                    if arguments[0] == "snapshot":
                        return subprocess.CompletedProcess(
                            arguments,
                            0,
                            stdout=json.dumps(
                                [
                                    {
                                        "backup-type": "host",
                                        "backup-id": "same",
                                        "backup-time": 100,
                                    }
                                ]
                            ),
                        )
                    target = Path(arguments[3])
                    target.mkdir()
                    (target / ".pbc-restore-sentinel").write_text(
                        "pbc-restore-sentinel-v1\n"
                    )
                    return subprocess.CompletedProcess(arguments, 0, stdout="")

                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(checker.subprocess, "run", side_effect=fake_run),
                    patch.object(checker.time, "time", return_value=200),
                ):
                    result = checker.verify()
                self.assertEqual(len(calls), 2)
                self.assertIn(f"namespace {expected or '<root>'}", result)

        with tempfile.TemporaryDirectory() as base:
            environment = {
                "REPO": "fake",
                "RESTORE_GROUP": "host/same",
                "BACKUP_NAME": "data.pxar",
                "RESTORE_TMP_BASE": base,
                "NAMESPACE": "photos",
            }
            calls = []

            def missing_snapshot(arguments):
                calls.append(arguments)
                # The root contains an identically named group; photos does not.
                if "--ns" not in arguments:
                    return json.dumps(
                        [
                            {
                                "backup-type": "host",
                                "backup-id": "same",
                                "backup-time": 100,
                            }
                        ]
                    )
                return "[]"

            with (
                patch.dict(os.environ, environment, clear=True),
                patch.object(checker, "client", side_effect=missing_snapshot),
                self.assertRaisesRegex(ValueError, "No snapshots found"),
            ):
                checker.verify()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-2:], ["--ns", "photos"])

    def test_encryption_and_config_fail_before_client(self):
        with tempfile.TemporaryDirectory() as base:
            environment = {
                "REPO": "fake",
                "RESTORE_GROUP": "host/test",
                "BACKUP_NAME": "data.pxar",
                "RESTORE_TMP_BASE": base,
                "ENCRYPTION_KEYFILE": base + "/key",
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch.object(checker, "client") as client,
            ):
                with self.assertRaisesRegex(ValueError, "Incomplete encryption"):
                    checker.verify()
                client.assert_not_called()
            for path in ["../sentinel", "*", "/sentinel", "nested/sentinel"]:
                environment.pop("ENCRYPTION_KEYFILE", None)
                environment["SENTINEL_PATH"] = path
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(checker, "client") as client,
                ):
                    with self.assertRaises(ValueError):
                        checker.verify()
                    client.assert_not_called()

    def test_notification_status(self):
        environment = {
            "RESTORE_GROUP": "host/test",
            "MSMTP_ACCOUNT": "default",
            "SENDER_EMAIL": "sender@example.com",
            "RECIPIENT_EMAIL": "to@example.com",
        }
        for failure in [False, True]:
            for mail_status in [0, 1]:
                with (
                    self.subTest(failure=failure, mail_status=mail_status),
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(checker, "verify") as verify,
                    patch.object(checker.subprocess, "run") as send,
                ):
                    if failure:
                        verify.side_effect = ValueError("secret must not appear")
                    else:
                        verify.return_value = "Restored"
                    send.return_value.returncode = mail_status
                    self.assertEqual(
                        checker.main(), 1 if failure else (2 if mail_status else 0)
                    )
                    self.assertNotIn("secret", send.call_args.kwargs["input"])
                    self.assertIn(
                        "failed" if failure else "verified", send.call_args.args[0][-1]
                    )


if __name__ == "__main__":
    unittest.main()
