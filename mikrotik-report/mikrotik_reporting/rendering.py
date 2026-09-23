"""Pure text rendering for weekly and monthly reports."""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .aggregation import (
    MIN_COMPARABLE_COVERAGE,
    comparable,
    coverage,
    month_window,
    week_window,
)
from .models import METRICS, Period, PeriodKind, PeriodWindow, PortDetection


def _comparison_line(label: str, current: int, previous: int) -> str:
    change = current - previous
    direction = "up" if change > 0 else "down" if change < 0 else "steady"
    percentage = f"{change / previous:+.1%}" if previous else "n/a (zero baseline)"
    return (
        f"  {label}: {current:,} vs {previous:,}; "
        f"{change:+,} ({percentage}, {direction})"
    )


def _window_for(period: Period, kind: PeriodKind) -> PeriodWindow:
    return (
        month_window(period["start"])
        if kind == "month"
        else week_window(period["start"])
    )


def _comparison_lines(
    period: Period,
    previous: Period | None,
    timezone_: ZoneInfo,
    window: PeriodWindow,
) -> list[str]:
    kind = window.kind
    lines = [f"{kind.title()} over {kind} (current vs previous)"]
    if not comparable(period, window, timezone_):
        return lines + [f"  Unavailable: current {kind} has low sample coverage."]
    if previous is None:
        return lines + [
            f"  Unavailable: previous calendar {kind} has no retained history."
        ]
    if not comparable(previous, _window_for(previous, kind), timezone_):
        return lines + [
            f"  Unavailable: previous calendar {kind} has low sample coverage."
        ]
    for source, label in (("local", "Local"), ("crowdsec", "CrowdSec")):
        for metric in METRICS:
            lines.append(
                _comparison_line(
                    f"{label} {metric}",
                    period["totals"][source][metric],
                    previous["totals"][source][metric],
                )
            )
        for size, description in (
            ("last_sizes", "latest list size"),
            ("max_sizes", "maximum list size"),
        ):
            lines.append(
                _comparison_line(
                    f"{label} {description}",
                    period[size][source],
                    previous[size][source],
                )
            )
    return lines


def _trend_lines(
    period: Period, history: dict[str, Period], timezone_: ZoneInfo
) -> list[str]:
    lines = ["Recent completed weeks (oldest to newest)"]
    start = date.fromisoformat(period["start"])
    for offset in (3, 2, 1, 0):
        week = (start - timedelta(days=7 * offset)).isoformat()
        item = period if offset == 0 else history.get(week)
        if item is None:
            lines.append(f"  {week}: unavailable (no retained history)")
            continue
        item_window = week_window(item["start"])
        expected, percentage = coverage(item, item_window, timezone_)
        if not comparable(item, item_window, timezone_):
            lines.append(
                f"  {week}: unavailable (low coverage: "
                f"{item['samples']:,} / ~{expected:,}, ~{percentage:.1f}%)"
            )
            continue
        local = item["totals"]["local"]
        crowdsec = item["totals"]["crowdsec"]
        lines.append(
            f"  {week}: local {local['packets']:,} packets / {local['bytes']:,} bytes; "
            f"CrowdSec {crowdsec['packets']:,} packets / {crowdsec['bytes']:,} bytes; "
            f"lists {item['last_sizes']['local']:,} / {item['last_sizes']['crowdsec']:,}; "
            f"coverage ~{percentage:.1f}%"
        )
    return lines


def _activity_lines(
    period: Period, timezone_: ZoneInfo, window: PeriodWindow
) -> list[str]:
    expected, percentage = coverage(period, window, timezone_)

    def value(number: int) -> str:
        return (
            f"{number:,}"
            if period["samples"] or window.kind not in ("month", "range")
            else "unavailable"
        )

    return [
        "Local MikroTik detection (configured local list)",
        f"  Packets dropped: {value(period['totals']['local']['packets'])}",
        f"  Bytes dropped: {value(period['totals']['local']['bytes'])}",
        (
            "  Address list size, latest / observed maximum: "
            f"{value(period['last_sizes']['local'])} / "
            f"{value(period['max_sizes']['local'])}"
        ),
        "",
        "CrowdSec decisions enforced by RouterOS bouncer",
        f"  Packets dropped: {value(period['totals']['crowdsec']['packets'])}",
        f"  Bytes dropped: {value(period['totals']['crowdsec']['bytes'])}",
        (
            "  Address list size, latest / observed maximum: "
            f"{value(period['last_sizes']['crowdsec'])} / "
            f"{value(period['max_sizes']['crowdsec'])}"
        ),
        "",
        "Data quality",
        f"  Samples: {period['samples']:,} / ~{expected:,} expected",
        f"  Coverage: ~{percentage:.1f}%",
        f"  Router reboots: {value(period['router_reboots'])}",
        f"  Counter resets: {value(period['counter_resets'])}",
        f"  Rule rebaselines: {value(period['rule_rebaselines'])}",
        "",
    ]


def _detected_port_lines(ports: list[PortDetection]) -> list[str]:
    lines = ["Top detected destination ports"]
    if not ports:
        return lines + ["  No detection events recorded.", ""]
    for item in ports:
        label = f"{item['destination_port']}/{item['protocol']}"
        detections = item["detections"]
        noun = "detection" if detections == 1 else "detections"
        lines.append(f"  {label:<12} {detections:>8,} {noun}")
    lines.append("")
    return lines


def _report_footer(period: Period, *, comparisons: bool = True) -> list[str]:
    lines = [
        (
            "Counters measure packets and bytes discarded by the selected rules, "
            "not unique IPs or attacks."
        ),
        "CrowdSec remains responsible for detecting and classifying attacks.",
        "Detected-port counts are detection events, not unique attacks or packets.",
    ]
    if comparisons:
        lines.append(
            "Expected samples and coverage are approximate; comparisons require at "
            f"least {MIN_COMPARABLE_COVERAGE:.0%} sample coverage in both periods."
        )
    else:
        lines.append("Expected samples and coverage are approximate.")
    if period["samples"] == 0:
        lines.extend(
            (
                "",
                (
                    "No collector samples were recorded; zero totals do not mean "
                    "zero blocked traffic."
                ),
            )
        )
    return lines


def render_weekly_report(
    period: Period,
    timezone_: ZoneInfo,
    history: dict[str, Period] | None = None,
    *,
    completed: bool = True,
    top_ports: list[PortDetection] | None = None,
) -> str:
    window = week_window(period["start"])
    lines = [
        (
            f"MikroTik blocking report: {window.start} to {window.end} "
            f"({timezone_.key}, end exclusive)"
        ),
        "",
        *_activity_lines(period, timezone_, window),
        *_detected_port_lines(top_ports or []),
    ]
    if completed:
        previous_start = (
            date.fromisoformat(period["start"]) - timedelta(days=7)
        ).isoformat()
        retained = history or {}
        lines.extend(
            _comparison_lines(period, retained.get(previous_start), timezone_, window)
        )
        lines.append("")
        lines.extend(_trend_lines(period, retained, timezone_))
        lines.append("")
    lines.extend(_report_footer(period))
    return "\n".join(lines) + "\n"


def render_monthly_report(
    period: Period,
    previous: Period | None,
    timezone_: ZoneInfo,
    top_ports: list[PortDetection] | None = None,
) -> str:
    window = month_window(period["start"])
    lines = [
        (
            f"MikroTik monthly blocking report: {window.start} to {window.end} "
            f"({timezone_.key}, end exclusive)"
        ),
        "",
        *_activity_lines(period, timezone_, window),
        *_detected_port_lines(top_ports or []),
        *_comparison_lines(period, previous, timezone_, window),
        "",
        *_report_footer(period),
    ]
    return "\n".join(lines) + "\n"


def render_range_report(
    period: Period,
    window: PeriodWindow,
    timezone_: ZoneInfo,
    top_ports: list[PortDetection] | None = None,
) -> str:
    if window.kind != "range":
        raise ValueError("Range report requires an explicit range window")
    lines = [
        (
            f"MikroTik blocking report: {window.start} to {window.end} "
            f"({timezone_.key}, start inclusive, end exclusive)"
        ),
        "",
        *_activity_lines(period, timezone_, window),
        *_detected_port_lines(top_ports or []),
    ]
    if not comparable(period, window, timezone_):
        lines.extend(
            (
                "Historical coverage",
                "  Unavailable: no persisted daily samples in the requested range."
                if period["samples"] == 0
                else "  Incomplete: totals include observed samples only.",
                "",
            )
        )
    lines.extend(
        (
            "Address-list latest values are from the last sampled day in the range.",
            *_report_footer(period, comparisons=False),
        )
    )
    return "\n".join(lines) + "\n"
