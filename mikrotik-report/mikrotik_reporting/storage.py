"""SQLite schema management and persistence for report state."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional, cast

from .aggregation import next_month
from .models import METRICS, SOURCES, Period, State, empty_period, initial_state

SCHEMA_VERSION = 1
HISTORY_WEEKS = 12


def _configure(database: sqlite3.Connection) -> sqlite3.Connection:
    database.row_factory = sqlite3.Row
    return database


def _schema_version(database: sqlite3.Connection) -> int:
    return int(database.execute("PRAGMA user_version").fetchone()[0])


def _check_supported_schema(database: sqlite3.Connection) -> int:
    version = _schema_version(database)
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"Database schema version {version} is newer than supported version "
            f"{SCHEMA_VERSION}"
        )
    return version


def _migrate_to_1(database: sqlite3.Connection) -> None:
    # This is also the migration path for databases created before explicit
    # schema versioning; every schema statement is intentionally idempotent.
    database.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS metadata (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_sample_at TEXT,
                last_uptime INTEGER
            );
            CREATE TABLE IF NOT EXISTS counters (
                rule_key TEXT PRIMARY KEY,
                packets INTEGER NOT NULL,
                bytes INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS periods (
                start TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN ('active', 'pending')),
                local_packets INTEGER NOT NULL,
                local_bytes INTEGER NOT NULL,
                crowdsec_packets INTEGER NOT NULL,
                crowdsec_bytes INTEGER NOT NULL,
                local_max_size INTEGER NOT NULL,
                crowdsec_max_size INTEGER NOT NULL,
                local_last_size INTEGER NOT NULL,
                crowdsec_last_size INTEGER NOT NULL,
                samples INTEGER NOT NULL,
                router_reboots INTEGER NOT NULL,
                counter_resets INTEGER NOT NULL,
                rule_rebaselines INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS weekly_history (
                start TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_aggregates (
                day TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS monthly_reports (
                start TEXT PRIMARY KEY,
                sent_at TEXT
            );
            PRAGMA user_version = 1;
            COMMIT;
        """)


MIGRATIONS = {1: _migrate_to_1}


def ensure_schema(database: sqlite3.Connection) -> None:
    version = _check_supported_schema(database)
    while version < SCHEMA_VERSION:
        target = version + 1
        MIGRATIONS[target](database)
        version = target


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = _configure(sqlite3.connect(path, timeout=30))
    os.chmod(path, 0o600)
    try:
        ensure_schema(database)
    except Exception:
        database.close()
        raise
    return database


def open_database_readonly(path: Path) -> sqlite3.Connection:
    database = _configure(
        sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)
    )
    try:
        _check_supported_schema(database)
    except Exception:
        database.close()
        raise
    return database


def open_database_existing(path: Path) -> sqlite3.Connection:
    database = _configure(
        sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=30)
    )
    try:
        ensure_schema(database)
    except Exception:
        database.close()
        raise
    return database


def load_day(database: sqlite3.Connection, day: str) -> Period:
    row = database.execute(
        "SELECT data FROM daily_aggregates WHERE day = ?", (day,)
    ).fetchone()
    return cast("Period", json.loads(row["data"])) if row else empty_period(day)


def save_day(database: sqlite3.Connection, day: Period) -> None:
    database.execute(
        "INSERT INTO daily_aggregates (day, data) VALUES (?, ?) "
        "ON CONFLICT(day) DO UPDATE SET data = excluded.data",
        (day["start"], json.dumps(day, sort_keys=True)),
    )


def aggregate_month(database: sqlite3.Connection, start: str) -> Optional[Period]:
    rows = database.execute(
        "SELECT data FROM daily_aggregates WHERE day >= ? AND day < ? ORDER BY day",
        (start, next_month(start)),
    ).fetchall()
    if not rows:
        return None
    month = empty_period(start)
    for row in rows:
        day = cast("Period", json.loads(row["data"]))
        for source in SOURCES:
            for metric in METRICS:
                month["totals"][source][metric] += day["totals"][source][metric]
            month["max_sizes"][source] = max(
                month["max_sizes"][source], day["max_sizes"][source]
            )
            month["last_sizes"][source] = day["last_sizes"][source]
        for field in (
            "samples",
            "router_reboots",
            "counter_resets",
            "rule_rebaselines",
        ):
            month[field] += day[field]
    return month


def queue_completed_months(database: sqlite3.Connection, current: str) -> None:
    first = database.execute("SELECT MIN(day) AS day FROM daily_aggregates").fetchone()
    if first is None or first["day"] is None:
        return
    month = date.fromisoformat(first["day"]).replace(day=1).isoformat()
    while month < current:
        database.execute(
            "INSERT OR IGNORE INTO monthly_reports (start) VALUES (?)", (month,)
        )
        month = next_month(month)


def next_pending_month(database: sqlite3.Connection, current: str) -> Optional[str]:
    row = database.execute(
        "SELECT start FROM monthly_reports "
        "WHERE sent_at IS NULL AND start < ? ORDER BY start LIMIT 1",
        (current,),
    ).fetchone()
    return cast("str", row["start"]) if row is not None else None


def mark_month_sent(
    database: sqlite3.Connection, start: str, sent_at: str
) -> None:
    database.execute(
        "UPDATE monthly_reports SET sent_at = ? WHERE start = ?", (sent_at, start)
    )


def load_history(database: sqlite3.Connection, start: str) -> dict[str, Period]:
    # Read-only previews may open a legacy database before a writing command migrates it.
    if not database.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'weekly_history'"
    ).fetchone():
        return {}
    rows = database.execute(
        "SELECT start, data FROM weekly_history WHERE start < ? ORDER BY start DESC LIMIT 3",
        (start,),
    ).fetchall()
    return {row["start"]: cast("Period", json.loads(row["data"])) for row in rows}


def retain_sent_week(database: sqlite3.Connection, period: Period) -> None:
    database.execute(
        "INSERT INTO weekly_history (start, data) VALUES (?, ?)",
        (period["start"], json.dumps(period, sort_keys=True)),
    )
    database.execute(
        "DELETE FROM weekly_history WHERE start NOT IN "
        "(SELECT start FROM weekly_history ORDER BY start DESC LIMIT ?)",
        (HISTORY_WEEKS,),
    )


def _period_from_row(row: sqlite3.Row) -> Period:
    period = empty_period(row["start"])
    for source in SOURCES:
        for metric in METRICS:
            period["totals"][source][metric] = row[f"{source}_{metric}"]
        period["max_sizes"][source] = row[f"{source}_max_size"]
        period["last_sizes"][source] = row[f"{source}_last_size"]
    for field in ("samples", "router_reboots", "counter_resets", "rule_rebaselines"):
        period[field] = row[field]
    return period


def load_state(database: sqlite3.Connection, start: str) -> State:
    meta = database.execute("SELECT * FROM metadata WHERE id = 1").fetchone()
    if meta is None:
        return initial_state(start)
    active = database.execute(
        "SELECT * FROM periods WHERE status = 'active'"
    ).fetchall()
    if len(active) != 1:
        raise ValueError("Database must contain exactly one active period")
    pending = database.execute(
        "SELECT * FROM periods WHERE status = 'pending' ORDER BY start"
    ).fetchall()
    counters = database.execute("SELECT * FROM counters").fetchall()
    return {
        "version": 1,
        "period": _period_from_row(active[0]),
        "pending": [_period_from_row(row) for row in pending],
        "counters": {
            row["rule_key"]: {"packets": row["packets"], "bytes": row["bytes"]}
            for row in counters
        },
        "last_sample_at": meta["last_sample_at"],
        "last_uptime": meta["last_uptime"],
    }


def save_state(database: sqlite3.Connection, state: State) -> None:
    database.execute("DELETE FROM metadata")
    database.execute(
        "INSERT INTO metadata VALUES (1, ?, ?)",
        (state["last_sample_at"], state["last_uptime"]),
    )
    database.execute("DELETE FROM counters")
    database.executemany(
        "INSERT INTO counters VALUES (?, ?, ?)",
        (
            (key, value["packets"], value["bytes"])
            for key, value in state["counters"].items()
        ),
    )
    database.execute("DELETE FROM periods")
    for period in state["pending"]:
        _insert_period(database, "pending", period)
    _insert_period(database, "active", state["period"])


def _insert_period(
    database: sqlite3.Connection, status: str, period: Period
) -> None:
    database.execute(
        "INSERT INTO periods VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            period["start"],
            status,
            period["totals"]["local"]["packets"],
            period["totals"]["local"]["bytes"],
            period["totals"]["crowdsec"]["packets"],
            period["totals"]["crowdsec"]["bytes"],
            period["max_sizes"]["local"],
            period["max_sizes"]["crowdsec"],
            period["last_sizes"]["local"],
            period["last_sizes"]["crowdsec"],
            period["samples"],
            period["router_reboots"],
            period["counter_resets"],
            period["rule_rebaselines"],
        ),
    )
