import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from helpers import UTC, at, common, mail, router, sample
from mikrotik_reporting.aggregation import apply_snapshot
from mikrotik_reporting.models import (
    DetectionBatch,
    DetectionEvent,
    empty_period,
    initial_state,
)
from mikrotik_reporting.rendering import render_monthly_report
from mikrotik_reporting.storage import (
    aggregate_month,
    load_history,
    load_state,
    open_database,
    open_database_existing,
    open_database_readonly,
    record_detection_batch,
    save_day,
    save_state,
)
from mikrotik_reporting.workflows import (
    collect,
    process_monthly_reports,
    process_weekly_reports,
    send_preview,
    send_weekly_reports,
)


class WorkflowTests(unittest.TestCase):
    def test_failed_weekly_mail_stays_pending_then_enters_history(self) -> None:
        state = initial_state("2026-09-14")
        apply_snapshot(state, sample(100, 10000), at(20), UTC)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            shared = common(path)
            delivery = mail(Path(temporary))
            with closing(open_database(path)) as database, database:
                save_state(database, state)
            with (
                closing(open_database_existing(path)) as database,
                database,
                patch(
                    "mikrotik_reporting.workflows._send_weekly_report",
                    side_effect=OSError("mail failed"),
                ),
                self.assertRaisesRegex(OSError, "mail failed"),
            ):
                process_weekly_reports(database, shared, delivery, at(21, 1))
            with closing(open_database(path)) as database:
                pending = load_state(database, "2026-09-21")["pending"]
                self.assertEqual([item["start"] for item in pending], ["2026-09-14"])
                self.assertEqual(load_history(database, "2026-09-21"), {})
            with (
                closing(open_database_existing(path)) as database,
                database,
                patch("mikrotik_reporting.workflows._send_weekly_report") as sender,
                redirect_stdout(StringIO()),
            ):
                process_weekly_reports(database, shared, delivery, at(21, 2))
                sender.assert_called_once()
            with closing(open_database(path)) as database:
                self.assertEqual(load_state(database, "2026-09-21")["pending"], [])
                self.assertEqual(
                    load_history(database, "2026-09-21")["2026-09-14"], pending[0]
                )

    def test_next_weekly_report_receives_sent_history(self) -> None:
        first = initial_state("2026-09-14")
        first["period"]["samples"] = 2016
        first["period"]["totals"]["local"]["packets"] = 10
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            shared = common(path)
            delivery = mail(Path(temporary))
            with closing(open_database(path)) as database, database:
                save_state(database, first)
                record_detection_batch(database, {"fingerprints": [], "events": []})
                record_detection_batch(
                    database,
                    {
                        "fingerprints": ["weekly-port"],
                        "events": [
                            {
                                "fingerprint": "weekly-port",
                                "day": "2026-09-15",
                                "source_ip": "192.0.2.10",
                                "protocol": "tcp",
                                "destination_port": 22,
                            }
                        ],
                    },
                )
            with (
                closing(open_database_existing(path)) as database,
                patch(
                    "mikrotik_reporting.workflows._send_weekly_report"
                ) as first_sender,
                redirect_stdout(StringIO()),
            ):
                process_weekly_reports(database, shared, delivery, at(21, 1))
                self.assertEqual(
                    first_sender.call_args.kwargs["top_ports"],
                    [
                        {
                            "protocol": "tcp",
                            "destination_port": 22,
                            "detections": 1,
                        }
                    ],
                )
                self.assertEqual(
                    first_sender.call_args.kwargs["top_sources"],
                    [
                        {
                            "source_ip": "192.0.2.10",
                            "detections": 1,
                        }
                    ],
                )
            with closing(open_database_existing(path)) as database, database:
                state = load_state(database, "2026-09-21")
                state["period"]["samples"] = 2016
                state["period"]["totals"]["local"]["packets"] = 20
                save_state(database, state)
            with (
                closing(open_database_existing(path)) as database,
                patch("mikrotik_reporting.workflows._send_weekly_report") as sender,
                redirect_stdout(StringIO()),
            ):
                process_weekly_reports(
                    database,
                    shared,
                    delivery,
                    datetime(2026, 9, 28, 1, tzinfo=timezone.utc),
                )
                history = sender.call_args.kwargs["history"]
                self.assertEqual(
                    history["2026-09-14"]["totals"]["local"]["packets"], 10
                )

    def test_daily_aggregates_split_month_inside_same_week(self) -> None:
        madrid = ZoneInfo("Europe/Madrid")
        times = (
            datetime(2026, 9, 30, 21, 45, tzinfo=timezone.utc),
            datetime(2026, 9, 30, 21, 50, tzinfo=timezone.utc),
            datetime(2026, 9, 30, 22, 10, tzinfo=timezone.utc),
        )
        snapshots = (
            sample(100, 10000, uptime=10000),
            sample(110, 11000, uptime=10300),
            sample(130, 13000, uptime=11500),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with (
                patch(
                    "mikrotik_reporting.workflows.fetch_snapshot",
                    side_effect=snapshots,
                ),
                patch(
                    "mikrotik_reporting.workflows.fetch_detection_batch",
                    return_value={"fingerprints": [], "events": []},
                ),
            ):
                for instant in times:
                    with redirect_stdout(StringIO()):
                        collect(common(path, madrid), router(), instant)
            with closing(open_database_readonly(path)) as database:
                september = aggregate_month(database, "2026-09-01")
                october = aggregate_month(database, "2026-10-01")
                weekly = load_state(database, "2026-09-28")["period"]
                assert september is not None and october is not None
                self.assertEqual(september["totals"]["local"]["packets"], 10)
                self.assertEqual(october["totals"]["local"]["packets"], 20)
                self.assertEqual(weekly["totals"]["local"]["packets"], 30)
                self.assertEqual(september["samples"], 2)
                self.assertEqual(october["samples"], 1)

    def test_collection_baselines_then_records_new_detection_logs(self) -> None:
        baseline_event: DetectionEvent = {
            "fingerprint": "baseline",
            "day": "2026-09-19",
            "source_ip": "192.0.2.30",
            "protocol": "tcp",
            "destination_port": 22,
        }
        new_event: DetectionEvent = {
            "fingerprint": "new",
            "day": "2026-09-19",
            "source_ip": "192.0.2.31",
            "protocol": "tcp",
            "destination_port": 23,
        }
        batches: tuple[DetectionBatch, DetectionBatch] = (
            {"fingerprints": ["baseline"], "events": [baseline_event]},
            {
                "fingerprints": ["baseline", "new"],
                "events": [baseline_event, new_event],
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with (
                patch(
                    "mikrotik_reporting.workflows.fetch_snapshot",
                    side_effect=(
                        sample(100, 10000, uptime=10000),
                        sample(110, 11000, uptime=10300),
                    ),
                ),
                patch(
                    "mikrotik_reporting.workflows.fetch_detection_batch",
                    side_effect=batches,
                ),
                redirect_stdout(StringIO()),
            ):
                collect(common(path), router(), at(19, 10))
                collect(common(path), router(), at(19, 11))
            with closing(open_database_readonly(path)) as database:
                ports = database.execute(
                    "SELECT protocol, destination_port, detections "
                    "FROM daily_detection_events"
                ).fetchall()
            self.assertEqual([tuple(row) for row in ports], [("tcp", 23, 1)])

    def test_monthly_mail_failure_retries_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            shared = common(path)
            delivery = mail(Path(temporary))
            day = empty_period("2026-09-15")
            day["samples"] = 1
            with closing(open_database(path)) as database, database:
                save_day(database, day)
            now = datetime(2026, 10, 1, 1, tzinfo=timezone.utc)
            with (
                closing(open_database_existing(path)) as database,
                database,
                patch(
                    "mikrotik_reporting.workflows._send_monthly_report",
                    side_effect=OSError("mail failed"),
                ),
                self.assertRaisesRegex(OSError, "mail failed"),
            ):
                process_monthly_reports(database, shared, delivery, now)
            with (
                closing(open_database_existing(path)) as database,
                patch("mikrotik_reporting.workflows._send_monthly_report") as sender,
                redirect_stdout(StringIO()),
            ):
                process_monthly_reports(database, shared, delivery, now)
                process_monthly_reports(database, shared, delivery, now)
                sender.assert_called_once()
                sent_at = database.execute(
                    "SELECT sent_at FROM monthly_reports"
                ).fetchone()["sent_at"]
                self.assertEqual(sent_at, now.isoformat())

    def test_monthly_sender_uses_previous_month_and_skips_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            shared = common(path)
            delivery = mail(Path(temporary))
            august = empty_period("2026-08-20")
            august["samples"] = 8928
            august["totals"]["local"]["packets"] = 10
            september = empty_period("2026-09-20")
            september["samples"] = 8640
            september["totals"]["local"]["packets"] = 30
            with closing(open_database(path)) as database, database:
                save_day(database, august)
                save_day(database, september)
                record_detection_batch(database, {"fingerprints": [], "events": []})
                record_detection_batch(
                    database,
                    {
                        "fingerprints": ["august-port", "september-port"],
                        "events": [
                            {
                                "fingerprint": "august-port",
                                "day": "2026-08-20",
                                "source_ip": "192.0.2.20",
                                "protocol": "udp",
                                "destination_port": 6881,
                            },
                            {
                                "fingerprint": "september-port",
                                "day": "2026-09-20",
                                "source_ip": "192.0.2.21",
                                "protocol": "tcp",
                                "destination_port": 23,
                            },
                        ],
                    },
                )
            with (
                closing(open_database_existing(path)) as database,
                patch("mikrotik_reporting.workflows._send_monthly_report") as sender,
                redirect_stdout(StringIO()),
            ):
                process_monthly_reports(
                    database,
                    shared,
                    delivery,
                    datetime(2026, 9, 30, 23, tzinfo=timezone.utc),
                )
                self.assertEqual(sender.call_args.args[2]["start"], "2026-08-01")
                self.assertEqual(
                    sender.call_args.kwargs["top_ports"][0]["destination_port"],
                    6881,
                )
                self.assertEqual(
                    sender.call_args.kwargs["top_sources"],
                    [
                        {
                            "source_ip": "192.0.2.20",
                            "detections": 1,
                        }
                    ],
                )
                process_monthly_reports(
                    database,
                    shared,
                    delivery,
                    datetime(2026, 10, 1, 1, tzinfo=timezone.utc),
                )
                self.assertEqual(sender.call_count, 2)
                current, previous = sender.call_args.args[2:]
                self.assertEqual(current["start"], "2026-09-01")
                self.assertEqual(previous["start"], "2026-08-01")
                self.assertEqual(
                    sender.call_args.kwargs["top_ports"][0]["destination_port"],
                    23,
                )
                self.assertEqual(
                    sender.call_args.kwargs["top_sources"],
                    [
                        {
                            "source_ip": "192.0.2.21",
                            "detections": 1,
                        }
                    ],
                )
                self.assertIn(
                    "Local packets: 30 vs 10; +20 (+200.0%, up)",
                    render_monthly_report(current, previous, UTC),
                )

    def test_monthly_sender_queues_whole_missing_month(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            shared = common(path)
            delivery = mail(Path(temporary))
            day = empty_period("2026-09-30")
            day["samples"] = 1
            with closing(open_database(path)) as database, database:
                save_day(database, day)
            with (
                closing(open_database_existing(path)) as database,
                patch("mikrotik_reporting.workflows._send_monthly_report") as sender,
                redirect_stdout(StringIO()),
            ):
                process_monthly_reports(
                    database,
                    shared,
                    delivery,
                    datetime(2026, 11, 1, 1, tzinfo=timezone.utc),
                )
                self.assertEqual(sender.call_count, 2)
                october = sender.call_args_list[1].args[2]
                self.assertEqual(october["start"], "2026-10-01")
                self.assertEqual(october["samples"], 0)
                self.assertIn(
                    "Packets dropped: unavailable",
                    render_monthly_report(october, None, UTC),
                )

    def test_report_before_first_collection_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "report.sqlite3"
            send_weekly_reports(common(path), mail(Path(temporary)), at(21, 1))
            self.assertFalse(path.parent.exists())

    def test_preview_sends_live_sample_without_changing_database(self) -> None:
        state = initial_state("2026-09-14")
        apply_snapshot(state, sample(100, 10000, uptime=10000), at(19, 10), UTC)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with closing(open_database(path)) as database, database:
                save_state(database, state)
            with (
                patch(
                    "mikrotik_reporting.workflows.fetch_snapshot",
                    return_value=sample(130, 13000, uptime=13600),
                ) as fetch,
                patch("mikrotik_reporting.workflows.subprocess.run") as mailer,
                redirect_stdout(StringIO()),
            ):
                send_preview(common(path), router(), mail(Path(temporary)), at(19, 11))
            fetch.assert_called_once()
            mailer.assert_called_once()
            args, kwargs = mailer.call_args
            subject = args[0][args[0].index("--subject") + 1]
            self.assertEqual(subject, "[TEST] MikroTik report (2026-09-14)")
            self.assertIn("--account", args[0])
            self.assertIn("TEST PREVIEW", kwargs["input"])
            self.assertIn("Packets dropped: 30", kwargs["input"])
            with closing(open_database_readonly(path)) as database:
                self.assertEqual(load_state(database, "2026-09-14"), state)


if __name__ == "__main__":
    unittest.main()
