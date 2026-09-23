import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from helpers import UTC, at, sample
from mikrotik_reporting.aggregation import (
    apply_snapshot,
    expected_samples,
    month_window,
    next_month,
    week_start,
    week_window,
)
from mikrotik_reporting.models import initial_state


class AggregationTests(unittest.TestCase):
    def test_deltas_resets_and_week_boundary(self) -> None:
        state = initial_state("2026-09-14")
        apply_snapshot(state, sample(100, 10000, uptime=10000), at(20, 10), UTC)
        apply_snapshot(state, sample(130, 13000, uptime=13600), at(20, 11), UTC)
        apply_snapshot(state, sample(5, 500, uptime=17200), at(20, 12), UTC)
        self.assertEqual(
            state["period"]["totals"]["local"], {"packets": 35, "bytes": 3500}
        )
        self.assertEqual(state["period"]["counter_resets"], 1)
        apply_snapshot(state, sample(10, 1000, uptime=64000), at(21, 1), UTC)
        self.assertEqual(state["pending"][0]["totals"]["local"]["packets"], 35)
        self.assertEqual(state["period"]["totals"]["local"]["packets"], 5)
        apply_snapshot(state, sample(3, 300, uptime=10), at(21, 2), UTC)
        self.assertEqual(
            state["period"]["totals"]["local"], {"packets": 8, "bytes": 800}
        )
        self.assertEqual(state["period"]["router_reboots"], 1)

    def test_new_rule_is_baselined(self) -> None:
        state = initial_state("2026-09-14")
        apply_snapshot(state, sample(100, 10000, uptime=10000), at(19), UTC)
        apply_snapshot(
            state, sample(50, 5000, uptime=13600, rule_id="*9"), at(19, 1), UTC
        )
        self.assertEqual(state["period"]["totals"]["local"]["packets"], 0)
        self.assertEqual(state["period"]["rule_rebaselines"], 1)

    def test_period_windows_follow_calendar_and_daylight_saving(self) -> None:
        madrid = ZoneInfo("Europe/Madrid")
        boundary = datetime(2026, 9, 20, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(week_start(boundary, UTC), "2026-09-14")
        self.assertEqual(week_start(boundary, madrid), "2026-09-21")
        self.assertEqual(expected_samples(week_window("2026-09-21"), madrid), 2016)
        self.assertEqual(expected_samples(week_window("2026-10-19"), madrid), 2028)
        self.assertEqual(expected_samples(week_window("2026-03-23"), madrid), 2004)
        self.assertEqual(next_month("2026-12-01"), "2027-01-01")
        self.assertEqual(next_month("2024-02-01"), "2024-03-01")
        self.assertEqual(expected_samples(month_window("2024-02-01"), madrid), 8352)
        self.assertEqual(expected_samples(month_window("2026-04-01"), madrid), 8640)
        self.assertEqual(expected_samples(month_window("2026-10-01"), madrid), 8940)
        self.assertEqual(expected_samples(month_window("2026-03-01"), madrid), 8916)


if __name__ == "__main__":
    unittest.main()
