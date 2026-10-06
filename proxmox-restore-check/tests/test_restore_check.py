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

    def exercise(
        self,
        kind="vm",
        fail_restore=False,
        fail_cleanup=False,
        bind=False,
        vm_cpu="",
        host_vcpus=4,
        max_test_vcpus=None,
        expected_cpu=None,
        max_age=0,
        guest_max_age=None,
        stamp="2026-01-01T00:00:00Z",
    ):
        cfg = self.config()
        cfg.max_test_vcpus = max_test_vcpus
        cfg.max_snapshot_age_seconds = max_age
        guest = cfg.guests[0 if kind == "vm" else 1]
        guest.max_snapshot_age_seconds = guest_max_age
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
                        [{"volid": f"pbs:backup/{kind}/{guest.source_id}/{stamp}"}]
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
                        + vm_cpu
                    )
                    if fail_restore:
                        raise rc.Failure("restore failed")
                if args[:2] == [tool, "start"]:
                    self.assertNotIn("net0:", path.read_text())
                    self.assertNotIn("hookscript:", path.read_text())
                    if expected_cpu is not None:
                        for line in expected_cpu:
                            self.assertIn(line + "\n", path.read_text())
                if args[:2] == [tool, "status"]:
                    return "status: running"
                if args[:2] == [tool, "destroy"]:
                    if fail_cleanup:
                        raise rc.Failure("destroy failed")
                    path.unlink()
                return ""

            with (
                patch.object(rc, "command", side_effect=fake),
                patch.object(rc, "host_cpu_capacity", return_value=host_vcpus),
                patch.object(rc.time, "time", return_value=1767312000),
            ):
                result = runner.test(guest)
        return result, calls

    def test_age_policy_before_restore_and_in_report(self):
        for kind in ("vm", "ct"):
            for limit, override, stamp, expected in (
                (86401, None, "2026-01-01T00:00:00Z", "OK"),
                (86400, None, "2026-01-01T00:00:00Z", "OK"),
                (86399, None, "2026-01-01T00:00:00Z", "FAIL"),
                (86399, 86400, "2026-01-01T00:00:00Z", "OK"),
                (86401, 86399, "2026-01-01T00:00:00Z", "FAIL"),
                (1, 0, "2020-01-01T00:00:00Z", "OK"),
                (0, None, "2020-01-01T00:00:00Z", "OK"),
                (0, None, "2026-01-02T00:05:00Z", "OK"),
                (0, None, "2026-01-02T00:05:01Z", "FAIL"),
            ):
                with self.subTest(
                    kind=kind, limit=limit, override=override, stamp=stamp
                ):
                    result, calls = self.exercise(
                        kind=kind,
                        max_age=limit,
                        guest_max_age=override,
                        stamp=stamp,
                    )
                    self.assertEqual(result[0], expected)
                    self.assertTrue(result[2])
                    self.assertIn(f"Backup: pbs:backup/{kind}/", result[1])
                    self.assertIn(stamp, result[1])
                    self.assertIn("Snapshot age:", result[1])
                    applied = limit if override is None else override
                    self.assertIn(
                        f"limit: {str(applied) + 's' if applied else 'disabled'}",
                        result[1],
                    )
                    if stamp == "2026-01-01T00:00:00Z":
                        self.assertIn("Snapshot age: 86400.0s", result[1])
                    restores = [
                        a
                        for a in calls
                        if a[0] == "qmrestore" or a[:2] == ["pct", "restore"]
                    ]
                    self.assertEqual(bool(restores), expected == "OK")
                    if expected == "FAIL":
                        self.assertEqual(len(calls), 1)

    def test_invalid_matching_backup_dates_are_rejected(self):
        for stamp in ("2026-02-30T00:00:00Z", "2026-01-01T25:00:00Z", "not-a-date"):
            with self.subTest(stamp=stamp):
                result, calls = self.exercise(stamp=stamp)
                self.assertEqual(result[0], "FAIL")
                self.assertIn("Invalid backup UTC date", result[1])
                self.assertIn(stamp, result[1])
                self.assertEqual(len(calls), 1)
        rows = [
            {"volid": "pbs:backup/vm/100/2026-01-01T00:00:00Z"},
            {"volid": "pbs:backup/vm/100/2026-02-30T00:00:00Z"},
        ]
        with self.assertRaises(rc.Failure):
            rc.latest_backup(json.dumps(rows), "pbs", self.config().guests[0])

    def test_age_failure_affects_summary_and_exit(self):
        cfg = self.config()
        cfg.guests = cfg.guests[:1]
        cfg.max_snapshot_age_seconds = 1
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            volume = "pbs:backup/vm/100/2026-01-01T00:00:00Z"
            with (
                patch.object(rc.Runner, "preflight"),
                patch.object(
                    rc.Runner, "paths", return_value=[base / "vm", base / "ct"]
                ),
                patch.object(rc.time, "time", return_value=1767312000),
                patch.object(
                    rc, "command", side_effect=[json.dumps([{"volid": volume}]), ""]
                ) as command,
            ):
                self.assertEqual(rc.run(cfg, True), 1)
            report = command.call_args.args[2]
            self.assertIn("FAIL=1", report)
            self.assertIn("Backup too old", report)
            self.assertIn("86400.0s; limit: 1s", report)
            self.assertIn(volume, report)
            self.assertEqual(command.call_count, 2)

    def test_config_age_validation_and_legacy_defaults(self):
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        original = json.loads(example.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for field in (
                "max_snapshot_age_seconds",
                "future_tolerance_seconds",
                "guest",
            ):
                for invalid in (-1, True, 1.5, "60", None):
                    if field == "guest" and invalid is None:
                        continue
                    data = json.loads(json.dumps(original))
                    if field == "guest":
                        data["guests"][0]["max_snapshot_age_seconds"] = invalid
                    else:
                        data[field] = invalid
                    path.write_text(json.dumps(data))
                    with self.assertRaisesRegex(
                        ValueError, "snapshot_age|future_tolerance"
                    ):
                        rc.load_config(path)
            original.pop("max_snapshot_age_seconds")
            original.pop("future_tolerance_seconds")
            original["guests"][0].pop("max_snapshot_age_seconds")
            path.write_text(json.dumps(original))
            cfg = rc.load_config(path)
            self.assertEqual(cfg.max_snapshot_age_seconds, 0)
            self.assertEqual(cfg.future_tolerance_seconds, 300)
            self.assertIsNone(cfg.guests[0].max_snapshot_age_seconds)
            cfg.future_tolerance_seconds = 0
            with (
                patch.object(rc.time, "time", return_value=1767312000),
                self.assertRaisesRegex(rc.Failure, "future"),
            ):
                rc.check_backup_age(
                    "pbs:backup/vm/100/2026-01-02T00:00:01Z", cfg, cfg.guests[0]
                )

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

    def test_large_vm_is_capped_before_start_and_reported(self):
        result, calls = self.exercise(
            vm_cpu="cores: 4\nsockets: 2\nvcpus: 8\n",
            expected_cpu=("cores: 4", "sockets: 1", "vcpus: 4"),
        )
        self.assertEqual(result[0], "OK")
        self.assertIn("CPU adjusted: 8 -> 4 vCPU; reason: DR host capacity", result[1])
        self.assertIn(["qm", "start", "999"], calls)

    def test_explicit_limit_caps_topology_and_hotplug_count(self):
        result, _ = self.exercise(
            vm_cpu="cores: 4\nsockets: 2\nvcpus: 3\n",
            host_vcpus=8,
            max_test_vcpus=2,
            expected_cpu=("cores: 2", "sockets: 1", "vcpus: 2"),
        )
        self.assertEqual(result[0], "OK")
        self.assertIn("initial vcpus: 3 -> 2", result[1])
        self.assertIn("reason: configured test limit", result[1])

    def test_vm_within_host_limit_keeps_cpu_fields(self):
        result, _ = self.exercise(
            vm_cpu="cores: 2\nsockets: 2\nvcpus: 3\n",
            expected_cpu=("cores: 2", "sockets: 2", "vcpus: 3"),
        )
        self.assertEqual(result[0], "OK")
        self.assertNotIn("CPU adjusted", result[1])

    def test_invalid_cpu_topology_fails_before_start(self):
        result, calls = self.exercise(vm_cpu="cores: 2\nsockets: 1\nvcpus: 3\n")
        self.assertEqual(result[0], "FAIL")
        self.assertNotIn(["qm", "start", "999"], calls)

    def test_host_cpu_capacity_respects_affinity(self):
        with (
            patch.object(rc.os, "cpu_count", return_value=8),
            patch.object(rc.os, "sched_getaffinity", return_value={0, 1, 2, 3}),
        ):
            self.assertEqual(rc.host_cpu_capacity(), 4)
        with (
            patch.object(rc.os, "cpu_count", return_value=None),
            patch.object(rc.os, "sched_getaffinity", return_value=set()),
            self.assertRaises(rc.Failure),
        ):
            rc.host_cpu_capacity()

    def test_config_rejects_invalid_test_cpu_limit(self):
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        data = json.loads(example.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for invalid in (0, -1, True, "4"):
                data["max_test_vcpus"] = invalid
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, "max_test_vcpus"):
                    rc.load_config(path)

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
