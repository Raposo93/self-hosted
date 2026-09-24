import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mikrotik_reporting.models import DetectionBatch, empty_period, initial_state
from mikrotik_reporting.storage import (
    HISTORY_WEEKS,
    SCHEMA_VERSION,
    load_history,
    load_state,
    open_database,
    open_database_existing,
    open_database_readonly,
    record_detection_batch,
    retain_sent_week,
    save_state,
    top_detected_ports,
    top_source_detections,
)


class StorageTests(unittest.TestCase):
    def test_state_survives_reopen_and_database_is_private(self) -> None:
        state = initial_state("2026-09-14")
        state["last_sample_at"] = "2026-09-14T00:00:00+00:00"
        state["last_uptime"] = 100
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with closing(open_database(path)) as database, database:
                save_state(database, state)
            with closing(open_database(path)) as database, database:
                self.assertEqual(load_state(database, "2026-09-14"), state)
                self.assertEqual(
                    database.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_sent_week_history_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with closing(open_database(path)) as database, database:
                for week in range(15):
                    start = datetime(2026, 1, 5, tzinfo=timezone.utc).date()
                    period = empty_period(
                        (start + timedelta(days=week * 7)).isoformat()
                    )
                    period["samples"] = 2016
                    retain_sent_week(database, period)
            with closing(open_database_existing(path)) as database:
                rows = database.execute(
                    "SELECT start FROM weekly_history ORDER BY start"
                ).fetchall()
                self.assertEqual(len(rows), HISTORY_WEEKS)
                self.assertEqual(rows[0]["start"], "2026-01-26")
                self.assertEqual(len(load_history(database, "2026-04-13")), 3)

    def test_detection_events_are_deduplicated_and_ranked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            baseline: DetectionBatch = {
                "fingerprints": ["old"],
                "events": [
                    {
                        "fingerprint": "old",
                        "day": "2026-09-30",
                        "source_ip": "192.0.2.1",
                        "protocol": "tcp",
                        "destination_port": 22,
                    }
                ],
            }
            new: DetectionBatch = {
                "fingerprints": ["old", "a", "b"],
                "events": [
                    baseline["events"][0],
                    {
                        "fingerprint": "a",
                        "day": "2026-09-30",
                        "source_ip": "192.0.2.2",
                        "protocol": "tcp",
                        "destination_port": 22,
                    },
                    {
                        "fingerprint": "b",
                        "day": "2026-10-01",
                        "source_ip": "192.0.2.3",
                        "protocol": "udp",
                        "destination_port": 6881,
                    },
                ],
            }
            repeated_port: DetectionBatch = {
                "fingerprints": ["a", "b", "c"],
                "events": [
                    new["events"][1],
                    new["events"][2],
                    {
                        "fingerprint": "c",
                        "day": "2026-10-01",
                        "source_ip": "192.0.2.4",
                        "protocol": "tcp",
                        "destination_port": 22,
                    },
                ],
            }
            with closing(open_database(path)) as database, database:
                self.assertEqual(record_detection_batch(database, baseline), 0)
                self.assertEqual(record_detection_batch(database, baseline), 0)
                self.assertEqual(record_detection_batch(database, new), 2)
                self.assertEqual(record_detection_batch(database, repeated_port), 1)
                combined = top_detected_ports(database, "2026-09-30", "2026-10-02")
                october = top_detected_ports(database, "2026-10-01", "2026-11-01")
                cursor_size = database.execute(
                    "SELECT COUNT(*) FROM detection_log_cursor"
                ).fetchone()[0]
                source_rows = database.execute(
                    "SELECT day, source_ip, detections "
                    "FROM daily_source_detections ORDER BY day, source_ip"
                ).fetchall()
            self.assertEqual(
                combined,
                [
                    {"protocol": "tcp", "destination_port": 22, "detections": 2},
                    {
                        "protocol": "udp",
                        "destination_port": 6881,
                        "detections": 1,
                    },
                ],
            )
            self.assertEqual(
                [tuple(row) for row in source_rows],
                [
                    ("2026-09-30", "192.0.2.2", 1),
                    ("2026-10-01", "192.0.2.3", 1),
                    ("2026-10-01", "192.0.2.4", 1),
                ],
            )
            self.assertEqual(
                october,
                [
                    {"protocol": "tcp", "destination_port": 22, "detections": 1},
                    {
                        "protocol": "udp",
                        "destination_port": 6881,
                        "detections": 1,
                    },
                ],
            )
            self.assertEqual(cursor_size, 3)

    def test_repeated_source_events_increment_only_for_new_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            first: DetectionBatch = {
                "fingerprints": ["a", "b"],
                "events": [
                    {
                        "fingerprint": "a",
                        "day": "2026-09-21",
                        "source_ip": "192.0.2.50",
                        "protocol": "tcp",
                        "destination_port": 22,
                    },
                    {
                        "fingerprint": "b",
                        "day": "2026-09-21",
                        "source_ip": "192.0.2.50",
                        "protocol": "tcp",
                        "destination_port": 23,
                    },
                ],
            }
            later: DetectionBatch = {
                "fingerprints": ["a", "b", "c"],
                "events": [
                    *first["events"],
                    {
                        "fingerprint": "c",
                        "day": "2026-09-22",
                        "source_ip": "192.0.2.50",
                        "protocol": "udp",
                        "destination_port": 6881,
                    },
                ],
            }

            with closing(open_database(path)) as database, database:
                self.assertEqual(
                    record_detection_batch(
                        database,
                        {"fingerprints": [], "events": []},
                    ),
                    0,
                )
                self.assertEqual(record_detection_batch(database, first), 2)
                self.assertEqual(record_detection_batch(database, first), 0)
                self.assertEqual(record_detection_batch(database, later), 1)

                daily = database.execute(
                    "SELECT day, source_ip, detections "
                    "FROM daily_source_detections ORDER BY day"
                ).fetchall()
                sources = top_source_detections(
                    database,
                    "2026-09-21",
                    "2026-09-23",
                )

            self.assertEqual(
                [tuple(row) for row in daily],
                [
                    ("2026-09-21", "192.0.2.50", 2),
                    ("2026-09-22", "192.0.2.50", 1),
                ],
            )
            self.assertEqual(
                sources,
                [{"source_ip": "192.0.2.50", "detections": 3}],
            )

    def test_router_reboot_resets_detection_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            event: DetectionBatch = {
                "fingerprints": ["reused-after-reboot"],
                "events": [
                    {
                        "fingerprint": "reused-after-reboot",
                        "day": "2026-10-01",
                        "source_ip": "192.0.2.5",
                        "protocol": "tcp",
                        "destination_port": 22,
                    }
                ],
            }
            with closing(open_database(path)) as database, database:
                self.assertEqual(record_detection_batch(database, event), 0)
                self.assertEqual(record_detection_batch(database, event), 0)
                self.assertEqual(
                    record_detection_batch(database, event, reset_cursor=True),
                    1,
                )
                ports = top_detected_ports(database, "2026-10-01", "2026-10-02")
                source = database.execute(
                    "SELECT source_ip, detections FROM daily_source_detections"
                ).fetchone()
            self.assertEqual(
                ports,
                [{"protocol": "tcp", "destination_port": 22, "detections": 1}],
            )
            self.assertEqual(tuple(source), ("192.0.2.5", 1))

    def test_legacy_database_is_migrated_without_losing_state(self) -> None:
        state = initial_state("2026-09-14")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with closing(open_database(path)) as database, database:
                save_state(database, state)
                database.execute("DROP TABLE weekly_history")
                database.execute("DROP TABLE daily_aggregates")
                database.execute("DROP TABLE monthly_reports")
                database.execute("PRAGMA user_version = 0")
            with closing(open_database_readonly(path)) as database:
                self.assertEqual(load_history(database, "2026-09-21"), {})
            with closing(open_database_existing(path)) as database:
                self.assertEqual(load_state(database, "2026-09-14"), state)
                self.assertEqual(
                    database.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
                for table in (
                    "weekly_history",
                    "daily_aggregates",
                    "monthly_reports",
                    "detection_log_state",
                    "detection_log_cursor",
                    "daily_detection_events",
                    "daily_source_detections",
                ):
                    self.assertIsNotNone(
                        database.execute(
                            "SELECT 1 FROM sqlite_master WHERE name = ?", (table,)
                        ).fetchone()
                    )

    def test_future_schema_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with closing(sqlite3.connect(path)) as database:
                database.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            with self.assertRaisesRegex(ValueError, "newer than supported"):
                open_database_existing(path)
            with self.assertRaisesRegex(ValueError, "newer than supported"):
                open_database_readonly(path)

    def test_source_detections_are_aggregated_and_ranked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"

            with closing(open_database(path)) as database, database:
                database.executemany(
                    "INSERT INTO daily_source_detections "
                    "(day, source_ip, detections) VALUES (?, ?, ?)",
                    [
                        ("2026-09-20", "192.0.2.10", 3),
                        ("2026-09-21", "192.0.2.10", 4),
                        ("2026-09-21", "192.0.2.20", 7),
                        ("2026-09-21", "192.0.2.30", 2),
                        ("2026-09-22", "192.0.2.40", 100),
                    ],
                )

                sources = top_source_detections(
                    database,
                    "2026-09-20",
                    "2026-09-22",
                )

            self.assertEqual(
                sources,
                [
                    {"source_ip": "192.0.2.10", "detections": 7},
                    {"source_ip": "192.0.2.20", "detections": 7},
                    {"source_ip": "192.0.2.30", "detections": 2},
                ],
            )


if __name__ == "__main__":
    unittest.main()
