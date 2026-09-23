"""Operational workflows combining RouterOS, SQLite, rendering, and mail."""

from __future__ import annotations

import sqlite3
import subprocess
from contextlib import closing
from datetime import date, datetime, timedelta

from .aggregation import (
    apply_snapshot,
    month_start,
    month_window,
    range_window,
    roll_period,
    week_start,
    week_window,
)
from .config import CommonConfig, MailConfig, RouterOSConfig
from .models import Period, PortDetection, SourceDetection, empty_period
from .rendering import (
    render_monthly_report,
    render_range_report,
    render_weekly_report,
)
from .routeros import fetch_detection_batch, fetch_snapshot
from .storage import (
    aggregate_month,
    aggregate_range,
    load_day,
    load_history,
    load_state,
    mark_month_sent,
    next_pending_month,
    open_database,
    open_database_existing,
    open_database_readonly,
    queue_completed_months,
    record_detection_batch,
    retain_sent_week,
    save_day,
    save_state,
    top_detected_ports,
    top_source_detections,
)


def _deliver_report(config: MailConfig, subject: str, body: str) -> None:
    command = [
        str(config.notifier),
        "--to",
        config.recipient,
        "--subject",
        subject,
    ]
    if config.account:
        command.extend(("--account", config.account))
    if config.sender:
        command.extend(("--from", config.sender))
    subprocess.run(command, input=body, text=True, check=True)


def _send_weekly_report(
    mail: MailConfig,
    common: CommonConfig,
    period: Period,
    preview_at: datetime | None = None,
    history: dict[str, Period] | None = None,
    top_ports: list[PortDetection] | None = None,
    top_sources: list[SourceDetection] | None = None,
) -> None:
    subject = f"{mail.subject} ({period['start']})"
    body = render_weekly_report(
        period,
        common.timezone,
        history,
        completed=preview_at is None,
        top_ports=top_ports,
        top_sources=top_sources,
    )
    if preview_at is not None:
        subject = f"[TEST] {subject}"
        body = (
            "TEST PREVIEW — incomplete reporting week.\n"
            f"Live RouterOS sample: {preview_at.isoformat()}\n"
            "SQLite state and scheduled reports were not changed.\n\n" + body
        )
    _deliver_report(mail, subject, body)


def _send_monthly_report(
    mail: MailConfig,
    common: CommonConfig,
    period: Period,
    previous: Period | None,
    top_ports: list[PortDetection] | None = None,
    top_sources: list[SourceDetection] | None = None,
) -> None:
    subject = f"{mail.subject} ({period['start'][:7]})"
    body = render_monthly_report(
        period, previous, common.timezone, top_ports, top_sources
    )
    _deliver_report(mail, subject, body)


def process_weekly_reports(
    database: sqlite3.Connection,
    common: CommonConfig,
    mail: MailConfig,
    now: datetime,
) -> None:
    current_week = week_start(now, common.timezone)
    database.execute("BEGIN IMMEDIATE")
    state = load_state(database, current_week)
    roll_period(state, current_week)
    save_state(database, state)
    database.commit()
    while state["pending"]:
        database.execute("BEGIN IMMEDIATE")
        # Reload after waiting for any concurrent collector.
        state = load_state(database, current_week)
        if not state["pending"]:
            database.commit()
            break
        period = state["pending"][0]
        history = load_history(database, period["start"])
        window = week_window(period["start"])
        ports = top_detected_ports(database, window.start, window.end)
        sources = top_source_detections(database, window.start, window.end)
        _send_weekly_report(
            mail, common, period, history=history, top_ports=ports, top_sources=sources
        )
        state["pending"].pop(0)
        save_state(database, state)
        retain_sent_week(database, period)
        database.commit()
        print(f"Sent report for week {period['start']}")


def process_monthly_reports(
    database: sqlite3.Connection,
    common: CommonConfig,
    mail: MailConfig,
    now: datetime,
) -> None:
    current_month = month_start(now, common.timezone)
    database.execute("BEGIN IMMEDIATE")
    queue_completed_months(database, current_month)
    database.commit()
    while True:
        database.execute("BEGIN IMMEDIATE")
        start = next_pending_month(database, current_month)
        if start is None:
            database.commit()
            break
        period = aggregate_month(database, start) or empty_period(start)
        previous_start = (
            (date.fromisoformat(start) - timedelta(days=1)).replace(day=1).isoformat()
        )
        previous = aggregate_month(database, previous_start)
        window = month_window(start)
        ports = top_detected_ports(database, window.start, window.end)
        sources = top_source_detections(database, window.start, window.end)
        _send_monthly_report(
            mail, common, period, previous, top_ports=ports, top_sources=sources
        )
        mark_month_sent(database, start, now.isoformat())
        database.commit()
        print(f"Sent report for month {start[:7]}")


def collect(common: CommonConfig, routeros: RouterOSConfig, now: datetime) -> None:
    snapshot = fetch_snapshot(routeros)
    detections = fetch_detection_batch(routeros, now, common.timezone)
    with closing(open_database(common.state)) as database:
        database.execute("BEGIN IMMEDIATE")
        state = load_state(database, week_start(now, common.timezone))
        day = load_day(database, now.astimezone(common.timezone).date().isoformat())
        router_reboot = apply_snapshot(state, snapshot, now, common.timezone, day)
        recorded_detections = record_detection_batch(
            database, detections, reset_cursor=router_reboot
        )
        save_state(database, state)
        save_day(database, day)
        database.commit()
        print(
            f"Collected {len(snapshot['counters'])} rules for week "
            f"{state['period']['start']} and {recorded_detections} detection events"
        )


def send_weekly_reports(common: CommonConfig, mail: MailConfig, now: datetime) -> None:
    if not common.state.is_file():
        return
    with closing(open_database_existing(common.state)) as database:
        process_weekly_reports(database, common, mail, now)


def send_monthly_reports(common: CommonConfig, mail: MailConfig, now: datetime) -> None:
    if not common.state.is_file():
        return
    with closing(open_database_existing(common.state)) as database:
        process_monthly_reports(database, common, mail, now)


def send_preview(
    common: CommonConfig,
    routeros: RouterOSConfig,
    mail: MailConfig,
    now: datetime,
) -> None:
    if not common.state.is_file():
        raise ValueError("Run collect before sending a test report")
    snapshot = fetch_snapshot(routeros)
    with closing(open_database_readonly(common.state)) as database:
        state = load_state(database, week_start(now, common.timezone))
        window = week_window(state["period"]["start"])
        ports = top_detected_ports(database, window.start, window.end)
        sources = top_source_detections(database, window.start, window.end)
    if state["last_sample_at"] is None:
        raise ValueError("Run collect before sending a test report")
    apply_snapshot(state, snapshot, now, common.timezone)
    _send_weekly_report(
        mail,
        common,
        state["period"],
        preview_at=now,
        top_ports=ports,
        top_sources=sources,
    )
    print(f"Sent test report for week {state['period']['start']} (state unchanged)")


def print_range_report(common: CommonConfig, start: date, end: date) -> None:
    window = range_window(start.isoformat(), end.isoformat())
    if not common.state.is_file():
        raise ValueError("Run collect before requesting a historical range")
    with closing(open_database_readonly(common.state)) as database:
        period = aggregate_range(database, window.start, window.end)
        ports = top_detected_ports(database, window.start, window.end)
        sources = top_source_detections(database, window.start, window.end)
    print(
        render_range_report(
            period or empty_period(window.start),
            window,
            common.timezone,
            ports,
            sources,
        ),
        end="",
    )
