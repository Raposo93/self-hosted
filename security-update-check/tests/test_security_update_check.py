import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "security_update_check", Path(__file__).parents[1] / "security_update_check.py"
)
assert spec and spec.loader
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def compare(left, right):
    return (left > right) - (left < right)


def origin(name="Debian", archive="stable-security", trusted=True):
    return NS(origin=name, archive=archive, trusted=trusted)


def package(
    name="openssl", origins=None, versions=None, candidate: str | None = "3", held=False
):
    return NS(
        name=name,
        fullname=f"{name}:amd64",
        installed=NS(version="1"),
        is_installed=True,
        selected_state=2 if held else 1,
        candidate=NS(version=candidate) if candidate else None,
        versions=versions or [NS(version="2", origins=origins or [origin()])],
    )


class UpdateTests(unittest.TestCase):
    def test_security_origins(self):
        for name, archive in (
            ("Debian", "oldstable-security"),
            ("Ubuntu", "noble-security"),
            ("UbuntuESM", "noble-infra-security"),
            ("UbuntuESMApps", "noble-apps-security"),
        ):
            with self.subTest(name=name):
                self.assertTrue(monitor.security_origin(origin(name, archive)))
        for entry in (
            origin(trusted=False),
            origin("Other"),
            origin(archive="stable-updates"),
        ):
            self.assertFalse(monitor.security_origin(entry))

    def test_superseded_security_and_held_updates_remain_visible(self):
        entry = package(held=True)
        entry.versions.insert(0, NS(version="3", origins=[origin(archive="stable")]))
        result = monitor.pending_updates(
            [entry], compare, False, frozenset({entry.name})
        )
        self.assertEqual(len(result), 1)
        self.assertIn("1 -> 2", result[0])
        self.assertIn("candidate: 3; held", result[0])

    def test_no_security_update(self):
        entries = [package(origins=[origin(archive="stable")]), package(candidate=None)]
        entries[1].versions[0].version = "1"
        uninstalled = package()
        uninstalled.is_installed = False
        entries.append(uninstalled)
        self.assertEqual(monitor.pending_updates(entries, compare, False), [])

    def test_proxmox_vendor_and_kernel_policy(self):
        entries = [
            package("qemu-server", [origin("Proxmox", "stable")]),
            package("proxmox-kernel-6.8", [origin(archive="stable")]),
            package("pve-kernel-5.15", [origin(archive="stable")]),
            package("libpve-storage-perl", [origin(archive="stable")]),
        ]
        self.assertEqual(len(monitor.pending_updates(entries, compare, True)), 4)
        self.assertEqual(monitor.pending_updates(entries, compare, False), [])
        entries[0].versions[0].origins[0].trusted = False
        self.assertEqual(monitor.pending_updates(entries[:1], compare, True), [])


class RebootTests(unittest.TestCase):
    def test_markers_and_kernel_without_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "vmlinuz-3-pve"
            image.touch()
            self.assertIn(
                "newer", monitor.reboot_reasons(compare, "2-pve", 0, root, root)[0]
            )
            (root / "reboot-required").touch()
            (root / "reboot-required.pkgs").write_text("libc6\n")
            reasons = monitor.reboot_reasons(compare, "3-pve", 0, root, root)
            self.assertEqual(len(reasons), 3)
            self.assertIn("libc6", reasons[1])
            self.assertIn("changed since boot", reasons[2])
            os.utime(image, (1, 1))
            self.assertEqual(
                len(monitor.reboot_reasons(compare, "3-pve", 2, root, root)), 2
            )

    def test_old_kernels_do_not_request_reboot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vmlinuz-1").touch()
            self.assertEqual(monitor.reboot_reasons(compare, "2", 0, root, root), [])


class ExecutionTests(unittest.TestCase):
    def run_main(
        self,
        updates=(),
        reboot=(),
        arguments=(),
        subprocess_error=None,
        notification_error=None,
    ):
        modules = {
            "apt": NS(Cache=dict),
            "apt_pkg": NS(
                version_compare=compare,
                Cache=lambda _: NS(packages=[]),
                SELSTATE_HOLD=2,
            ),
        }
        with (
            patch.object(sys, "argv", ["check", *arguments]),
            patch.dict(os.environ, {"RECIPIENT_EMAIL": "test@example.com"}),
            patch.object(
                monitor.importlib, "import_module", side_effect=modules.__getitem__
            ),
            patch.object(monitor, "pending_updates", return_value=list(updates)),
            patch.object(monitor, "reboot_reasons", return_value=list(reboot)),
            patch.object(
                monitor.subprocess, "run", side_effect=subprocess_error
            ) as run,
            patch.object(
                monitor, "send_notification", side_effect=notification_error
            ) as send,
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = monitor.main()
            return result, output.getvalue(), run.call_args_list, send.call_args_list

    def test_quiet_success_refreshes(self):
        result, output, calls, sent = self.run_main()
        self.assertEqual((result, output, sent), (0, "", []))
        self.assertIn("APT::Update::Error-Mode=any", calls[0].args[0])

    def test_attention_sends_and_dry_run_does_not(self):
        for updates, reboot in (
            (["openssl"], []),
            ([], ["kernel"]),
            (["openssl"], ["kernel"]),
        ):
            result, output, _, sent = self.run_main(updates, reboot)
            self.assertEqual(result, 0)
            self.assertEqual(len(sent), 1)
            self.assertIn("Host:", output)
        result, output, calls, sent = self.run_main(
            ["openssl"], arguments=["--no-refresh", "--dry-run"]
        )
        self.assertEqual((result, calls, sent), (0, [], []))
        self.assertIn("SECURITY UPDATES", output)

    def test_failed_refresh_never_reports_success(self):
        result, output, _, sent = self.run_main(
            subprocess_error=subprocess.CalledProcessError(100, "apt-get")
        )
        self.assertEqual((result, output, sent), (1, "", []))

    def test_notifier_interface_and_failure(self):
        with (
            patch.dict(
                os.environ,
                {
                    "RECIPIENT_EMAIL": "test@example.com",
                    "MSMTP_ACCOUNT": "notifications",
                },
            ),
            patch.object(monitor.subprocess, "run") as run,
        ):
            monitor.send_notification("REBOOT REQUIRED", "body", "host")
            self.assertTrue(
                run.call_args.args[0][0].endswith("mail-notifier/send-mail.sh")
            )
            self.assertEqual(run.call_args.kwargs["input"], "body")
            run.side_effect = subprocess.CalledProcessError(1, "notifier")
            with self.assertRaises(subprocess.CalledProcessError):
                monitor.send_notification("REBOOT REQUIRED", "body", "host")

    def test_mail_failure_fails_service(self):
        result, output, _, sent = self.run_main(
            ["openssl"], notification_error=subprocess.CalledProcessError(1, "notifier")
        )
        self.assertEqual(result, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("SECURITY UPDATES", output)


if __name__ == "__main__":
    unittest.main()
