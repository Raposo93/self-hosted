import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mikrotik_reporting.models import empty_period, initial_state
from mikrotik_reporting.storage import (
    HISTORY_WEEKS,
    SCHEMA_VERSION,
    load_history,
    load_state,
    open_database,
    open_database_existing,
    open_database_readonly,
    retain_sent_week,
    save_state,
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
                    database.execute("PRAGMA user_version").fetchone()[0], 1
                )
                for table in (
                    "weekly_history",
                    "daily_aggregates",
                    "monthly_reports",
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


if __name__ == "__main__":
    unittest.main()
