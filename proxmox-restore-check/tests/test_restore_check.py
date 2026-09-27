import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "proxmox_restore_check.py"
spec = importlib.util.spec_from_file_location("restore_check", MODULE)
assert spec and spec.loader
rc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rc
spec.loader.exec_module(rc)


class RestoreTests(unittest.TestCase):
    def config(self):
        return rc.Config(
            "dr",
            "pbs",
            "local-lvm",
            999,
            "operator@example.com",
            [rc.Guest("vm", 100, "vm", True), rc.Guest("ct", 101, "ct")],
        )

    def test_latest_filters_type_id_storage_and_sorts(self):
        rows = [
            {"volid": v}
            for v in [
                "pbs:backup/vm/100/2026-01-01T00:00:00Z",
                "pbs:backup/ct/100/2026-09-01T00:00:00Z",
                "other:backup/vm/100/2026-09-01T00:00:00Z",
                "pbs:backup/vm/100/2026-02-01T00:00:00Z",
            ]
        ]
        self.assertEqual(
            rc.latest_backup(json.dumps(rows), "pbs", self.config().guests[0]),
            rows[-1]["volid"],
        )
        with self.assertRaises(rc.Failure):
            rc.latest_backup("[]", "pbs", self.config().guests[0])

    def test_sanitize_vm_removes_all_network_and_host_access(self):
        source = "scsi0: local-lvm:vm-999-disk-0\nnet0: virtio,bridge=vmbr0\nhostpci0: 01:00\nusb0: host=1\nargs: -netdev user\nhookscript: local:script\nserial0: /dev/ttyS0\nide2: local:iso/missing.iso,media=cdrom\nonboot: 1\nprotection: 1\n"
        clean, removed = rc.sanitize(source, "vm", "local-lvm")
        self.assertEqual(
            clean, "scsi0: local-lvm:vm-999-disk-0\nonboot: 0\nprotection: 0\n"
        )
        self.assertIn("net0", removed)
        self.assertIn("hookscript", removed)

    def test_sanitize_ct_rejects_bind_and_foreign_disks(self):
        for source in [
            "mp0: /host/data,mp=/data",
            "rootfs: production:disk",
            "[snapshot]",
        ]:
            with self.assertRaises(rc.Failure):
                rc.sanitize(source, "ct", "local-lvm")
        clean, _ = rc.sanitize(
            "rootfs: local-lvm:vm-999-disk-0\nnet0: bridge=vmbr0\nlxc.mount.entry: /host\ndev0: /dev/sda\n",
            "ct",
            "local-lvm",
        )
        self.assertNotIn("net0", clean)
        self.assertNotIn("lxc.mount", clean)
        self.assertNotIn("dev0", clean)

    def exercise(self, kind="vm", fail_restore=False, fail_cleanup=False, bind=False):
        cfg = self.config()
        guest = cfg.guests[0 if kind == "vm" else 1]
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            runner = rc.Runner(cfg)
            runner.base = Path(tmp)
            for folder in ("qemu-server", "lxc"):
                (runner.base / folder).mkdir()
            path = runner.paths()[0 if kind == "vm" else 1]
            tool = "qm" if kind == "vm" else "pct"

            def fake(args, timeout=60, body=None):
                calls.append(args)
                if args[:2] == ["pvesh", "get"]:
                    return json.dumps(
                        [
                            {
                                "volid": f"pbs:backup/{kind}/{guest.source_id}/2026-01-01T00:00:00Z"
                            }
                        ]
                    )
                if args[:2] == ["pvesm", "extractconfig"]:
                    return (
                        "mp0: /host/data,mp=/data"
                        if bind
                        else "rootfs: production:disk"
                    )
                if args[0] == "qmrestore" or args[:2] == ["pct", "restore"]:
                    path.write_text(
                        ("scsi0" if kind == "vm" else "rootfs")
                        + ": local-lvm:vm-999-disk-0\nnet0: bridge=vmbr0\nhookscript: local:hook\n"
                    )
                    if fail_restore:
                        raise rc.Failure("restore failed")
                if args[:2] == [tool, "start"]:
                    self.assertNotIn("net0:", path.read_text())
                    self.assertNotIn("hookscript:", path.read_text())
                if args[:2] == [tool, "status"]:
                    return "status: running"
                if args[:2] == [tool, "destroy"]:
                    if fail_cleanup:
                        raise rc.Failure("destroy failed")
                    path.unlink()
                return ""

            with patch.object(rc, "command", side_effect=fake):
                result = runner.test(guest)
        return result, calls

    def test_vm_and_ct_isolated_before_start_and_cleaned(self):
        for kind in ("vm", "ct"):
            result, calls = self.exercise(kind)
            self.assertEqual(result[0], "OK")
            self.assertTrue(result[2])
            self.assertIn(
                ["qm", "agent", "999", "ping"]
                if kind == "vm"
                else ["pct", "exec", "999", "--", "/bin/true"],
                calls,
            )
            self.assertEqual(calls[-1][1], "destroy")

    def test_partial_restore_is_cleaned_without_start(self):
        result, calls = self.exercise(fail_restore=True)
        self.assertEqual(result[0], "FAIL")
        self.assertTrue(result[2])
        self.assertNotIn(["qm", "start", "999"], calls)
        self.assertEqual(calls[-1][1], "destroy")

    def test_cleanup_failure_is_unsafe(self):
        result, _ = self.exercise(fail_cleanup=True)
        self.assertFalse(result[2])
        self.assertEqual(result[0], "FAIL")

    def test_bind_mount_refused_before_restore(self):
        result, calls = self.exercise(kind="ct", bind=True)
        self.assertEqual(result[0], "FAIL")
        self.assertFalse(any(args[:2] == ["pct", "restore"] for args in calls))

    def test_existing_id_is_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = rc.Runner(self.config())
            runner.base = Path(tmp)
            runner.paths()[0].parent.mkdir()
            runner.paths()[0].write_text("existing guest")
            with patch.object(rc, "command") as command:
                self.assertFalse(runner.test(self.config().guests[0])[2])
                command.assert_not_called()

    def test_report_once_then_poweroff_and_skip_unsafe(self):
        cfg = self.config()
        cfg.poweroff = True
        with (
            patch.object(rc.Runner, "preflight"),
            patch.object(
                rc.Runner, "test", return_value=("FAIL", "cleanup failed", False)
            ) as test,
            patch.object(rc, "command") as command,
        ):
            self.assertEqual(rc.run(cfg, False), 1)
            self.assertEqual(test.call_count, 1)
            self.assertEqual(command.call_count, 2)
            self.assertIn("SKIP=1", command.call_args_list[0].args[0][-1])
            self.assertEqual(
                command.call_args_list[1].args[0], ["systemctl", "poweroff"]
            )

    def test_guest_failure_continues_and_mail_failure_preserves_poweroff(self):
        cfg = self.config()
        cfg.poweroff = True
        with (
            patch.object(rc.Runner, "preflight"),
            patch.object(
                rc.Runner,
                "test",
                side_effect=[("FAIL", "bad", True), ("OK", "good", True)],
            ) as test,
            patch.object(
                rc, "command", side_effect=[rc.Failure("mail"), ""]
            ) as command,
        ):
            self.assertEqual(rc.run(cfg, False), 1)
            self.assertEqual(test.call_count, 2)
            self.assertEqual(
                command.call_args_list[-1].args[0], ["systemctl", "poweroff"]
            )

    def test_no_poweroff_override(self):
        cfg = self.config()
        cfg.poweroff = True
        with (
            patch.object(rc.Runner, "preflight"),
            patch.object(rc.Runner, "test", return_value=("OK", "good", True)),
            patch.object(rc, "command") as command,
        ):
            self.assertEqual(rc.run(cfg, True), 0)
            self.assertEqual(command.call_count, 1)

    def test_command_timeout(self):
        with self.assertRaises(rc.Failure):
            rc.command([sys.executable, "-c", "import time; time.sleep(30)"], 0.05)


if __name__ == "__main__":
    unittest.main()
