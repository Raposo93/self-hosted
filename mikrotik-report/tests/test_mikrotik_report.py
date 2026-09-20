import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mikrotik_report as report

UTC = ZoneInfo("UTC")


def _sample(
    packets: int,
    bytes_: int,
    uptime: int = 100,
    rule_id: str = "*1",
    local_size: int = 2,
    crowd_size: int = 3,
) -> report._Snapshot:
    return {
        "counters": {
            f"local:raw:{rule_id}": {"packets": packets, "bytes": bytes_},
            "crowdsec:filter:*2": {"packets": 10, "bytes": 1000},
        },
        "sizes": {"local": local_size, "crowdsec": crowd_size},
        "uptime": uptime,
    }


def _at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


class ReportTests(unittest.TestCase):
    def test_fetch_snapshot_selects_only_matching_drop_rules(self) -> None:
        cfg = {
            "local_table": "raw",
            "local_comment": "Drop WAN scanners",
            "local_list": "wan-scanners",
            "crowdsec_signature": "@cs-routeros-bouncer",
            "crowdsec_list": "crowdsec-banned",
        }
        calls: list[tuple[str, dict[str, str]]] = []

        def _fake_get(
            _cfg: dict[str, Any], path: str, params: dict[str, str]
        ) -> object:
            calls.append((path, params))
            if path == "ip/firewall/raw":
                return [
                    {
                        ".id": "*1",
                        "comment": "Drop WAN scanners",
                        "action": "drop",
                        "src-address-list": "wan-scanners",
                        "packets": "12",
                        "bytes": "1200",
                    },
                    {
                        ".id": "*3",
                        "comment": "unrelated",
                        "action": "drop",
                        "src-address-list": "other",
                        "packets": "900",
                        "bytes": "90000",
                    },
                ]
            if path == "ip/firewall/filter":
                return [
                    {
                        ".id": "*2",
                        "comment": "crowdsec-bouncer:filter-input @cs-routeros-bouncer",
                        "action": "drop",
                        "src-address-list": "crowdsec-banned",
                        "packets": "5",
                        "bytes": "500",
                    },
                    {
                        ".id": "*4",
                        "comment": "@cs-routeros-bouncer",
                        "action": "drop",
                        "src-address-list": "other",
                        "packets": "90",
                        "bytes": "9000",
                    },
                ]
            if path == "ip/firewall/address-list":
                return (
                    [{".id": "*A"}, {".id": "*B"}]
                    if params["list"] == "wan-scanners"
                    else [{".id": "*C"}]
                )
            if path == "system/resource":
                return {"uptime": "1w2d03:04:05"}
            raise AssertionError(path)

        with patch.object(report, "_get_json", side_effect=_fake_get):
            snapshot = report._fetch_snapshot(cfg)
        self.assertEqual(
            snapshot["counters"],
            {
                "local:raw:*1": {"packets": 12, "bytes": 1200},
                "crowdsec:filter:*2": {"packets": 5, "bytes": 500},
            },
        )
        self.assertEqual(snapshot["sizes"], {"local": 2, "crowdsec": 1})
        self.assertEqual(snapshot["uptime"], 9 * 86400 + 3 * 3600 + 4 * 60 + 5)
        self.assertEqual(len(calls), 5)

    def test_deltas_resets_and_week_boundary(self) -> None:
        state = report._initial_state("2026-09-14")
        report._apply_snapshot(
            state, _sample(100, 10000, uptime=10000), _at(20, 10), UTC
        )
        report._apply_snapshot(
            state, _sample(130, 13000, uptime=13600), _at(20, 11), UTC
        )
        report._apply_snapshot(state, _sample(5, 500, uptime=17200), _at(20, 12), UTC)
        self.assertEqual(
            state["period"]["totals"]["local"], {"packets": 35, "bytes": 3500}
        )
        self.assertEqual(state["period"]["counter_resets"], 1)
        report._apply_snapshot(state, _sample(10, 1000, uptime=64000), _at(21, 1), UTC)
        self.assertEqual(state["pending"][0]["totals"]["local"]["packets"], 35)
        self.assertEqual(state["period"]["totals"]["local"]["packets"], 5)
        report._apply_snapshot(state, _sample(3, 300, uptime=10), _at(21, 2), UTC)
        self.assertEqual(
            state["period"]["totals"]["local"], {"packets": 8, "bytes": 800}
        )
        self.assertEqual(state["period"]["router_reboots"], 1)

    def test_new_rule_is_baselined_and_state_survives_reopen(self) -> None:
        state = report._initial_state("2026-09-14")
        report._apply_snapshot(state, _sample(100, 10000, uptime=10000), _at(19), UTC)
        report._apply_snapshot(
            state, _sample(50, 5000, uptime=13600, rule_id="*9"), _at(19, 1), UTC
        )
        self.assertEqual(state["period"]["totals"]["local"]["packets"], 0)
        self.assertEqual(state["period"]["rule_rebaselines"], 1)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with closing(report._open_database(path)) as database, database:
                report._save_state(database, state)
            with closing(report._open_database(path)) as database, database:
                restored = report._load_state(database, "2026-09-14")
            self.assertEqual(restored, state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_local_rule_fails(self) -> None:
        cfg = {
            "local_table": "raw",
            "local_comment": "missing",
            "local_list": "wan-scanners",
            "crowdsec_signature": "@cs-routeros-bouncer",
            "crowdsec_list": "crowdsec-banned",
        }
        with (
            patch.object(report, "_get_json", return_value=[]),
            self.assertRaisesRegex(ValueError, "exactly one local drop rule"),
        ):
            report._fetch_snapshot(cfg)

    def test_failed_mail_keeps_completed_week_pending_for_retry(self) -> None:
        state = report._initial_state("2026-09-14")
        report._apply_snapshot(state, _sample(100, 10000), _at(20), UTC)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with closing(report._open_database(path)) as database, database:
                report._save_state(database, state)
            cfg = {"timezone": UTC}
            with (
                closing(report._open_database(path)) as database,
                database,
                patch.object(
                    report, "_send_report", side_effect=OSError("mail failed")
                ),
                self.assertRaisesRegex(OSError, "mail failed"),
            ):
                report._process_report(database, cfg, _at(21, 1))
            with closing(report._open_database(path)) as database, database:
                pending = report._load_state(database, "2026-09-21")["pending"]
                self.assertEqual([item["start"] for item in pending], ["2026-09-14"])
                self.assertEqual(report._load_history(database, "2026-09-21"), {})
                with (
                    patch.object(report, "_send_report") as sender,
                    redirect_stdout(StringIO()),
                ):
                    report._process_report(database, cfg, _at(21, 2))
                    sender.assert_called_once()
            with closing(report._open_database(path)) as database, database:
                self.assertEqual(
                    report._load_state(database, "2026-09-21")["pending"], []
                )
                self.assertEqual(
                    report._load_history(database, "2026-09-21")["2026-09-14"],
                    pending[0],
                )

    def test_weekly_comparison_zero_baseline_and_missing_history(self) -> None:
        previous = report._empty_period("2026-09-14")
        current = report._empty_period("2026-09-21")
        previous["samples"] = current["samples"] = 2016
        current["totals"]["local"]["packets"] = 50
        current["totals"]["local"]["bytes"] = 5000
        current["totals"]["crowdsec"]["packets"] = 20
        previous["totals"]["crowdsec"]["packets"] = 10
        current["last_sizes"]["local"] = 4
        previous["last_sizes"]["local"] = 2
        rendered = report._render_report(current, UTC, {previous["start"]: previous})
        self.assertIn("Local packets: 50 vs 0; +50 (n/a (zero baseline), up)", rendered)
        self.assertIn("CrowdSec packets: 20 vs 10; +10 (+100.0%, up)", rendered)
        self.assertIn("Local latest list size: 4 vs 2; +2 (+100.0%, up)", rendered)
        self.assertIn("Samples: 2,016 / ~2,016 expected", rendered)
        self.assertIn("Coverage: ~100.0%", rendered)
        self.assertIn("2026-09-07: unavailable (no retained history)", rendered)
        self.assertIn("2026-09-14: local 0 packets", rendered)
        missing = report._render_report(current, UTC)
        self.assertIn("previous calendar week has no retained history", missing)

    def test_low_coverage_history_is_not_compared_as_zero(self) -> None:
        previous = report._empty_period("2026-09-14")
        current = report._empty_period("2026-09-21")
        previous["samples"] = 40
        current["samples"] = 2016
        rendered = report._render_report(current, UTC, {previous["start"]: previous})
        self.assertIn("previous calendar week has low sample coverage", rendered)
        self.assertIn(
            "2026-09-14: unavailable (low coverage: 40 / ~2,016, ~2.0%)", rendered
        )
        self.assertNotIn("Local packets: 0 vs 0", rendered)

    def test_expected_samples_follow_calendar_week_and_daylight_saving(self) -> None:
        madrid = ZoneInfo("Europe/Madrid")
        boundary = datetime(2026, 9, 20, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(report._week_start(boundary, UTC), "2026-09-14")
        self.assertEqual(report._week_start(boundary, madrid), "2026-09-21")
        self.assertEqual(report._expected_samples("2026-09-21", madrid), 2016)
        self.assertEqual(report._expected_samples("2026-10-19", madrid), 2028)
        self.assertEqual(report._expected_samples("2026-03-23", madrid), 2004)

    def test_sent_week_history_survives_reopen_and_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with closing(report._open_database(path)) as database, database:
                for week in range(15):
                    start = datetime(2026, 1, 5, tzinfo=timezone.utc).date()
                    period = report._empty_period(
                        (start + timedelta(days=week * 7)).isoformat()
                    )
                    period["samples"] = 2016
                    report._retain_sent_week(database, period)
            with closing(report._open_database_existing(path)) as database:
                rows = database.execute(
                    "SELECT start FROM weekly_history ORDER BY start"
                ).fetchall()
                self.assertEqual(len(rows), report.HISTORY_WEEKS)
                self.assertEqual(rows[0]["start"], "2026-01-26")
                self.assertEqual(len(report._load_history(database, "2026-04-13")), 3)

    def test_next_report_uses_previously_sent_week_after_reopen(self) -> None:
        first = report._initial_state("2026-09-14")
        first["period"]["samples"] = 2016
        first["period"]["totals"]["local"]["packets"] = 10
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            cfg = {"timezone": UTC}
            with closing(report._open_database(path)) as database, database:
                report._save_state(database, first)
            with (
                closing(report._open_database_existing(path)) as database,
                patch.object(report, "_send_report"),
                redirect_stdout(StringIO()),
            ):
                report._process_report(database, cfg, _at(21, 1))
            with closing(report._open_database_existing(path)) as database, database:
                state = report._load_state(database, "2026-09-21")
                state["period"]["samples"] = 2016
                state["period"]["totals"]["local"]["packets"] = 20
                report._save_state(database, state)
            with (
                closing(report._open_database_existing(path)) as database,
                patch.object(report, "_send_report") as sender,
                redirect_stdout(StringIO()),
            ):
                report._process_report(
                    database, cfg, datetime(2026, 9, 28, 1, tzinfo=timezone.utc)
                )
                history = sender.call_args.kwargs["history"]
                self.assertEqual(
                    history["2026-09-14"]["totals"]["local"]["packets"], 10
                )
                current = sender.call_args.args[1]
                self.assertIn(
                    "Local packets: 20 vs 10; +10 (+100.0%, up)",
                    report._render_report(current, UTC, history),
                )

    def test_existing_database_gets_history_table_without_losing_state(self) -> None:
        state = report._initial_state("2026-09-14")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with closing(report._open_database(path)) as database, database:
                report._save_state(database, state)
                database.execute("DROP TABLE weekly_history")
            with closing(report._open_database_readonly(path)) as database:
                self.assertEqual(report._load_history(database, "2026-09-21"), {})
            with closing(report._open_database_existing(path)) as database:
                self.assertEqual(report._load_state(database, "2026-09-14"), state)
                self.assertEqual(report._load_history(database, "2026-09-21"), {})

    def test_report_before_first_collection_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "report.sqlite3"
            report._report({"state": path, "timezone": UTC}, _at(21, 1))
            self.assertFalse(path.parent.exists())

    def test_test_report_sends_live_preview_without_changing_sqlite(self) -> None:
        state = report._initial_state("2026-09-14")
        report._apply_snapshot(
            state, _sample(100, 10000, uptime=10000), _at(19, 10), UTC
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with closing(report._open_database(path)) as database, database:
                report._save_state(database, state)
            cfg = {
                "state": path,
                "timezone": UTC,
                "notifier": Path(temporary) / "send-mail.sh",
                "recipient": "test@example.net",
                "subject": "MikroTik report",
                "account": "test-account",
                "from": "",
            }
            with (
                patch.object(
                    report,
                    "_fetch_snapshot",
                    return_value=_sample(130, 13000, uptime=13600),
                ) as fetch,
                patch.object(report.subprocess, "run") as mailer,
                redirect_stdout(StringIO()),
            ):
                report._test_report(cfg, _at(19, 11))
            fetch.assert_called_once_with(cfg)
            mailer.assert_called_once()
            args, kwargs = mailer.call_args
            subject = args[0][args[0].index("--subject") + 1]
            self.assertEqual(subject, "[TEST] MikroTik report (2026-09-14)")
            self.assertIn("--account", args[0])
            self.assertIn("TEST PREVIEW", kwargs["input"])
            self.assertIn("Packets dropped: 30", kwargs["input"])
            with closing(report._open_database_readonly(path)) as database:
                self.assertEqual(report._load_state(database, "2026-09-14"), state)


if __name__ == "__main__":
    unittest.main()
