"""Pure calendar, counter-delta, and data-quality calculations."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import (
    METRICS,
    SOURCES,
    Period,
    PeriodWindow,
    Snapshot,
    State,
    empty_period,
)

COLLECTION_INTERVAL_SECONDS = 5 * 60
MIN_COMPARABLE_COVERAGE = 0.90


def week_start(now: datetime, timezone_: ZoneInfo) -> str:
    local = now.astimezone(timezone_).date()
    return (local - timedelta(days=local.weekday())).isoformat()


def month_start(now: datetime, timezone_: ZoneInfo) -> str:
    return now.astimezone(timezone_).date().replace(day=1).isoformat()


def next_month(start: str) -> str:
    month = date.fromisoformat(start)
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1).isoformat()


def week_window(start: str) -> PeriodWindow:
    end = (date.fromisoformat(start) + timedelta(days=7)).isoformat()
    return PeriodWindow(start=start, end=end, kind="week")


def month_window(start: str) -> PeriodWindow:
    return PeriodWindow(start=start, end=next_month(start), kind="month")


def day_window(start: str) -> PeriodWindow:
    end = (date.fromisoformat(start) + timedelta(days=1)).isoformat()
    return PeriodWindow(start=start, end=end, kind="day")


def range_window(start: str, end: str) -> PeriodWindow:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if first >= last:
        raise ValueError("Range start must be before its exclusive end")
    return PeriodWindow(start=start, end=end, kind="range")


def roll_period(state: State, start: str) -> None:
    current = state["period"]["start"]
    while current < start:
        state["pending"].append(state["period"])
        current = (date.fromisoformat(current) + timedelta(days=7)).isoformat()
        state["period"] = empty_period(current)
    if current > start:
        raise ValueError("Clock moved into an earlier reporting week")


def apply_snapshot(
    state: State,
    snapshot: Snapshot,
    now: datetime,
    timezone_: ZoneInfo,
    daily: Period | None = None,
) -> None:
    roll_period(state, week_start(now, timezone_))
    period = state["period"]
    aggregates = (period, daily) if daily is not None else (period,)
    previous_at = state["last_sample_at"]
    reboot = False
    if previous_at is not None:
        elapsed = (now - datetime.fromisoformat(previous_at)).total_seconds()
        if elapsed < 0:
            raise ValueError("Clock moved backwards since the last sample")
        previous_uptime = state["last_uptime"]
        if previous_uptime is None:
            raise ValueError("State is missing the previous router uptime")
        reboot = (
            snapshot["uptime"] < previous_uptime or snapshot["uptime"] + 120 < elapsed
        )
    if reboot:
        for aggregate in aggregates:
            aggregate["router_reboots"] += 1
    for key, current in snapshot["counters"].items():
        previous = state["counters"].get(key)
        if previous_at is None or previous is None:
            if previous_at is not None:
                for aggregate in aggregates:
                    aggregate["rule_rebaselines"] += 1
            continue
        source = key.split(":", 1)[0]
        reset = reboot or any(current[metric] < previous[metric] for metric in METRICS)
        if reset and not reboot:
            for aggregate in aggregates:
                aggregate["counter_resets"] += 1
        for metric in METRICS:
            difference = (
                current[metric] if reset else current[metric] - previous[metric]
            )
            for aggregate in aggregates:
                aggregate["totals"][source][metric] += difference
    state["counters"] = snapshot["counters"]
    for source in SOURCES:
        size = snapshot["sizes"][source]
        for aggregate in aggregates:
            aggregate["last_sizes"][source] = size
            aggregate["max_sizes"][source] = max(aggregate["max_sizes"][source], size)
    for aggregate in aggregates:
        aggregate["samples"] += 1
    state["last_sample_at"] = now.isoformat()
    state["last_uptime"] = snapshot["uptime"]


def expected_samples(window: PeriodWindow, timezone_: ZoneInfo) -> int:
    first = datetime.combine(
        date.fromisoformat(window.start), time.min, tzinfo=timezone_
    )
    last = datetime.combine(date.fromisoformat(window.end), time.min, tzinfo=timezone_)
    seconds = (
        last.astimezone(timezone.utc) - first.astimezone(timezone.utc)
    ).total_seconds()
    return round(seconds / COLLECTION_INTERVAL_SECONDS)


def coverage(
    period: Period, window: PeriodWindow, timezone_: ZoneInfo
) -> tuple[int, float]:
    expected = expected_samples(window, timezone_)
    return expected, 100 * period["samples"] / expected


def comparable(period: Period, window: PeriodWindow, timezone_: ZoneInfo) -> bool:
    expected, _ = coverage(period, window, timezone_)
    return period["samples"] >= expected * MIN_COMPARABLE_COVERAGE
