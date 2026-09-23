import argparse
import os
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from helpers import common
from mikrotik_reporting.aggregation import expected_samples, range_window
from mikrotik_reporting.cli import _date_argument, main
from mikrotik_reporting.models import empty_period
from mikrotik_reporting.storage import (
    aggregate_range,
    open_database,
    record_detection_batch,
    save_day,
)
from mikrotik_reporting.workflows import print_range_report


class RangeReportTests(unittest.TestCase):
    def test_range_aggregates_exact_dates_across_month_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            september = empty_period("2026-09-30")
            september["samples"] = 288
            september["totals"]["local"]["packets"] = 10
            september["max_sizes"]["local"] = 4
            september["last_sizes"]["local"] = 3
            october = empty_period("2026-10-01")
            october["samples"] = 288
            october["totals"]["local"]["packets"] = 20
            october["max_sizes"]["local"] = 7
            october["last_sizes"]["local"] = 6
            with closing(open_database(path)) as database, database:
                save_day(database, september)
                save_day(database, october)
                combined = aggregate_range(database, "2026-09-30", "2026-10-02")
                october_only = aggregate_range(database, "2026-10-01", "2026-10-02")
            assert combined is not None and october_only is not None
            self.assertEqual(combined["totals"]["local"]["packets"], 30)
            self.assertEqual(combined["samples"], 576)
            self.assertEqual(combined["max_sizes"]["local"], 7)
            self.assertEqual(combined["last_sizes"]["local"], 6)
            self.assertEqual(october_only["totals"]["local"]["packets"], 20)

    def test_partial_week_exposes_missing_historical_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            observed = empty_period("2026-09-16")
            observed["samples"] = 288
            observed["totals"]["local"]["packets"] = 42
            observed["router_reboots"] = 1
            observed["counter_resets"] = 2
            observed["rule_rebaselines"] = 3
            with closing(open_database(path)) as database, database:
                save_day(database, observed)
            output = StringIO()
            with redirect_stdout(output):
                print_range_report(common(path), date(2026, 9, 16), date(2026, 9, 19))
            rendered = output.getvalue()
            self.assertIn("start inclusive, end exclusive", rendered)
            self.assertIn("Packets dropped: 42", rendered)
            self.assertIn("Samples: 288 / ~864 expected", rendered)
            self.assertIn("Coverage: ~33.3%", rendered)
            self.assertIn("Router reboots: 1", rendered)
            self.assertIn("Counter resets: 2", rendered)
            self.assertIn("Rule rebaselines: 3", rendered)
            self.assertIn("Incomplete: totals include observed samples only", rendered)

    def test_range_expected_samples_follow_timezone_and_dst(self) -> None:
        madrid = ZoneInfo("Europe/Madrid")
        window = range_window("2026-10-24", "2026-10-26")
        self.assertEqual(expected_samples(window, madrid), 588)

    def test_empty_historical_range_is_unavailable_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            with closing(open_database(path)):
                pass
            output = StringIO()
            with redirect_stdout(output):
                print_range_report(common(path), date(2025, 1, 1), date(2025, 1, 3))
            rendered = output.getvalue()
            self.assertIn("Packets dropped: unavailable", rendered)
            self.assertIn("Samples: 0 / ~576 expected", rendered)
            self.assertIn("no persisted daily samples", rendered)

    def test_invalid_ranges_and_dates_fail(self) -> None:
        for start, end in (
            ("2026-09-01", "2026-09-01"),
            ("2026-09-02", "2026-09-01"),
        ):
            with (
                self.subTest(start=start, end=end),
                self.assertRaisesRegex(ValueError, "start must be before"),
            ):
                range_window(start, end)
        with self.assertRaisesRegex(argparse.ArgumentTypeError, "expected YYYY-MM-DD"):
            _date_argument("2026-02-30")

    def test_cli_range_needs_no_router_or_mail_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.sqlite3"
            day = empty_period("2026-09-16")
            day["samples"] = 1
            with closing(open_database(path)) as database, database:
                save_day(database, day)
                record_detection_batch(database, {"fingerprints": [], "events": []})
                record_detection_batch(
                    database,
                    {
                        "fingerprints": ["range-port"],
                        "events": [
                            {
                                "fingerprint": "range-port",
                                "day": "2026-09-16",
                                "source_ip": "192.0.2.40",
                                "protocol": "tcp",
                                "destination_port": 22,
                            }
                        ],
                    },
                )
            arguments = [
                "mikrotik_report.py",
                "range",
                "--from",
                "2026-09-16",
                "--to",
                "2026-09-17",
            ]
            with (
                patch.object(sys, "argv", arguments),
                patch.dict(
                    os.environ,
                    {"MIKROTIK_REPORT_DB": str(path)},
                    clear=True,
                ),
                redirect_stdout(StringIO()) as output,
                redirect_stderr(StringIO()),
            ):
                main()
            self.assertIn("2026-09-16 to 2026-09-17", output.getvalue())
            self.assertIn("192.0.2.40", output.getvalue())
            self.assertIn("22/tcp", output.getvalue())


if __name__ == "__main__":
    unittest.main()
