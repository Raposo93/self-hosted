import unittest

from helpers import UTC
from mikrotik_reporting.models import empty_period
from mikrotik_reporting.rendering import render_monthly_report, render_weekly_report


class RenderingTests(unittest.TestCase):
    def test_weekly_comparison_zero_baseline_and_missing_history(self) -> None:
        previous = empty_period("2026-09-14")
        current = empty_period("2026-09-21")
        previous["samples"] = current["samples"] = 2016
        current["totals"]["local"]["packets"] = 50
        current["totals"]["local"]["bytes"] = 5000
        current["totals"]["crowdsec"]["packets"] = 20
        previous["totals"]["crowdsec"]["packets"] = 10
        current["last_sizes"]["local"] = 4
        previous["last_sizes"]["local"] = 2
        rendered = render_weekly_report(
            current, UTC, {previous["start"]: previous}
        )
        self.assertIn("Local packets: 50 vs 0; +50 (n/a (zero baseline), up)", rendered)
        self.assertIn("CrowdSec packets: 20 vs 10; +10 (+100.0%, up)", rendered)
        self.assertIn("Local latest list size: 4 vs 2; +2 (+100.0%, up)", rendered)
        self.assertIn("Samples: 2,016 / ~2,016 expected", rendered)
        self.assertIn("Coverage: ~100.0%", rendered)
        self.assertIn("2026-09-07: unavailable (no retained history)", rendered)
        self.assertIn("2026-09-14: local 0 packets", rendered)
        self.assertIn(
            "previous calendar week has no retained history",
            render_weekly_report(current, UTC),
        )

    def test_low_coverage_history_is_not_compared_as_zero(self) -> None:
        previous = empty_period("2026-09-14")
        current = empty_period("2026-09-21")
        previous["samples"] = 40
        current["samples"] = 2016
        rendered = render_weekly_report(current, UTC, {previous["start"]: previous})
        self.assertIn("previous calendar week has low sample coverage", rendered)
        self.assertIn(
            "2026-09-14: unavailable (low coverage: 40 / ~2,016, ~2.0%)",
            rendered,
        )
        self.assertNotIn("Local packets: 0 vs 0", rendered)

    def test_monthly_comparison_and_missing_history(self) -> None:
        previous = empty_period("2026-08-01")
        current = empty_period("2026-09-01")
        previous["samples"] = 8928
        current["samples"] = 8640
        previous["totals"]["local"]["packets"] = 10
        current["totals"]["local"]["packets"] = 30
        current["totals"]["crowdsec"]["bytes"] = 100
        rendered = render_monthly_report(current, previous, UTC)
        self.assertIn("2026-09-01 to 2026-10-01 (UTC, end exclusive)", rendered)
        self.assertIn("Month over month", rendered)
        self.assertIn("Local packets: 30 vs 10; +20 (+200.0%, up)", rendered)
        self.assertIn(
            "CrowdSec bytes: 100 vs 0; +100 (n/a (zero baseline), up)", rendered
        )
        self.assertIn("Samples: 8,640 / ~8,640 expected", rendered)
        self.assertIn(
            "previous calendar month has no retained history",
            render_monthly_report(current, None, UTC),
        )
        previous["samples"] = 20
        self.assertIn(
            "previous calendar month has low sample coverage",
            render_monthly_report(current, previous, UTC),
        )

    def test_empty_month_marks_activity_unavailable(self) -> None:
        rendered = render_monthly_report(empty_period("2026-10-01"), None, UTC)
        self.assertIn("Packets dropped: unavailable", rendered)
        self.assertIn("zero totals do not mean zero blocked traffic", rendered)


if __name__ == "__main__":
    unittest.main()
