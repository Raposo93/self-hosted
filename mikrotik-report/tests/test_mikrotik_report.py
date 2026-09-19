import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mikrotik_report as report


UTC = ZoneInfo("UTC")


def sample(packets, bytes_, uptime=100, rule_id="*1", local_size=2, crowd_size=3):
    return {
        "counters": {
            f"local:raw:{rule_id}": {"packets": packets, "bytes": bytes_},
            "crowdsec:filter:*2": {"packets": 10, "bytes": 1000},
        },
        "sizes": {"local": local_size, "crowdsec": crowd_size},
        "uptime": uptime,
    }


def at(day, hour=0):
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


class ReportTests(unittest.TestCase):
    def test_fetch_snapshot_selects_only_matching_drop_rules(self):
        cfg = {
            "local_table": "raw", "local_comment": "Drop WAN scanners",
            "local_list": "wan-scanners", "crowdsec_signature": "@cs-routeros-bouncer",
            "crowdsec_list": "crowdsec-banned",
        }
        calls = []

        def fake_get(_cfg, path, params):
            calls.append((path, params))
            if path == "ip/firewall/raw":
                return [
                    {".id": "*1", "comment": "Drop WAN scanners", "action": "drop",
                     "src-address-list": "wan-scanners", "packets": "12", "bytes": "1200"},
                    {".id": "*3", "comment": "unrelated", "action": "drop",
                     "src-address-list": "other", "packets": "900", "bytes": "90000"},
                ]
            if path == "ip/firewall/filter":
                return [
                    {".id": "*2", "comment": "crowdsec-bouncer:filter-input @cs-routeros-bouncer",
                     "action": "drop", "src-address-list": "crowdsec-banned",
                     "packets": "5", "bytes": "500"},
                    {".id": "*4", "comment": "@cs-routeros-bouncer", "action": "drop",
                     "src-address-list": "other", "packets": "90", "bytes": "9000"},
                ]
            if path == "ip/firewall/address-list":
                return [{".id": "*A"}, {".id": "*B"}] if params["list"] == "wan-scanners" else [{".id": "*C"}]
            if path == "system/resource":
                return {"uptime": "1w2d03:04:05"}
            raise AssertionError(path)

        with patch.object(report, "get_json", side_effect=fake_get):
            snapshot = report.fetch_snapshot(cfg)
        self.assertEqual(snapshot["counters"], {
            "local:raw:*1": {"packets": 12, "bytes": 1200},
            "crowdsec:filter:*2": {"packets": 5, "bytes": 500},
        })
        self.assertEqual(snapshot["sizes"], {"local": 2, "crowdsec": 1})
        self.assertEqual(snapshot["uptime"], 9 * 86400 + 3 * 3600 + 4 * 60 + 5)
        self.assertEqual(len(calls), 5)

    def test_deltas_resets_and_week_boundary(self):
        state = report.initial_state("2026-09-14")
        report.apply_snapshot(state, sample(100, 10000, uptime=10000), at(20, 10), UTC)
        report.apply_snapshot(state, sample(130, 13000, uptime=13600), at(20, 11), UTC)
        report.apply_snapshot(state, sample(5, 500, uptime=17200), at(20, 12), UTC)
        self.assertEqual(state["period"]["totals"]["local"], {"packets": 35, "bytes": 3500})
        self.assertEqual(state["period"]["counter_resets"], 1)
        report.apply_snapshot(state, sample(10, 1000, uptime=64000), at(21, 1), UTC)
        self.assertEqual(state["pending"][0]["totals"]["local"]["packets"], 35)
        self.assertEqual(state["period"]["totals"]["local"]["packets"], 5)
        report.apply_snapshot(state, sample(3, 300, uptime=10), at(21, 2), UTC)
        self.assertEqual(state["period"]["totals"]["local"], {"packets": 8, "bytes": 800})
        self.assertEqual(state["period"]["router_reboots"], 1)

    def test_new_rule_is_baselined_and_state_survives_reopen(self):
        state = report.initial_state("2026-09-14")
        report.apply_snapshot(state, sample(100, 10000, uptime=10000), at(19), UTC)
        report.apply_snapshot(state, sample(50, 5000, uptime=13600, rule_id="*9"), at(19, 1), UTC)
        self.assertEqual(state["period"]["totals"]["local"]["packets"], 0)
        self.assertEqual(state["period"]["rule_rebaselines"], 1)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with report.open_database(path) as database:
                report.save_state(database, state)
            with report.open_database(path) as database:
                restored = report.load_state(database, "2026-09-14")
            self.assertEqual(restored, state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_local_rule_fails(self):
        cfg = {"local_table": "raw", "local_comment": "missing", "local_list": "wan-scanners",
               "crowdsec_signature": "@cs-routeros-bouncer", "crowdsec_list": "crowdsec-banned"}
        with patch.object(report, "get_json", return_value=[]):
            with self.assertRaisesRegex(ValueError, "exactly one local drop rule"):
                report.fetch_snapshot(cfg)

    def test_failed_mail_keeps_completed_week_pending_for_retry(self):
        state = report.initial_state("2026-09-14")
        report.apply_snapshot(state, sample(100, 10000), at(20), UTC)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with report.open_database(path) as database:
                report.save_state(database, state)
            cfg = {"timezone": UTC}
            with report.open_database(path) as database:
                with patch.object(report, "send_report", side_effect=OSError("mail failed")):
                    with self.assertRaisesRegex(OSError, "mail failed"):
                        report.process_report(database, cfg, at(21, 1))
            with report.open_database(path) as database:
                pending = report.load_state(database, "2026-09-21")["pending"]
                self.assertEqual([item["start"] for item in pending], ["2026-09-14"])
                with patch.object(report, "send_report") as sender, redirect_stdout(StringIO()):
                    report.process_report(database, cfg, at(21, 2))
                    sender.assert_called_once()
            with report.open_database(path) as database:
                self.assertEqual(report.load_state(database, "2026-09-21")["pending"], [])


if __name__ == "__main__":
    unittest.main()
