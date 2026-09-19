"""Collect RouterOS drop counters and mail completed weekly summaries."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict, cast
from zoneinfo import ZoneInfo

SOURCES = ("local", "crowdsec")
METRICS = ("packets", "bytes")
RULE_FIELDS = ".id,comment,action,src-address-list,packets,bytes,disabled,invalid"
UPTIME_PART = re.compile(r"(\d+)([ywdhms])")


class _Snapshot(TypedDict):
    counters: dict[str, dict[str, int]]
    sizes: dict[str, int]
    uptime: int


class _Period(TypedDict):
    start: str
    totals: dict[str, dict[str, int]]
    max_sizes: dict[str, int]
    last_sizes: dict[str, int]
    samples: int
    router_reboots: int
    counter_resets: int
    rule_rebaselines: int


class _State(TypedDict):
    version: int
    period: _Period
    pending: list[_Period]
    counters: dict[str, dict[str, int]]
    last_sample_at: str | None
    last_uptime: int | None


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be set")
    return value


def _config(report: bool = False) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "state": Path(_required("MIKROTIK_REPORT_DB")),
        "timezone": ZoneInfo(os.environ.get("MIKROTIK_REPORT_TIMEZONE", "UTC")),
    }
    if not cfg["state"].is_absolute():
        raise ValueError("MIKROTIK_REPORT_DB must be an absolute path")
    if report:
        cfg["recipient"] = _required("MIKROTIK_REPORT_TO")
        cfg["subject"] = os.environ.get(
            "MIKROTIK_REPORT_SUBJECT", "MikroTik weekly blocking report"
        )
        cfg["notifier"] = (
            Path(__file__).resolve().parent.parent / "mail-notifier/send-mail.sh"
        )
        cfg["account"] = os.environ.get("MIKROTIK_REPORT_MAIL_ACCOUNT", "")
        cfg["from"] = os.environ.get("MIKROTIK_REPORT_FROM", "")
    else:
        url = _required("MIKROTIK_REST_URL").rstrip("/")
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path != "/rest"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "MIKROTIK_REST_URL must be an HTTPS URL ending in /rest, without credentials"
            )
        cfg.update(
            url=url,
            user=_required("MIKROTIK_USER"),
            password=_required("MIKROTIK_PASSWORD"),
            local_list=_required("MIKROTIK_LOCAL_LIST"),
            local_comment=_required("MIKROTIK_LOCAL_RULE_COMMENT"),
            crowdsec_list=_required("MIKROTIK_CROWDSEC_LIST"),
            crowdsec_signature=_required("MIKROTIK_CROWDSEC_RULE_SIGNATURE"),
        )
        cfg["local_table"] = os.environ.get("MIKROTIK_LOCAL_RULE_TABLE", "raw")
        if cfg["local_table"] not in ("raw", "filter"):
            raise ValueError("MIKROTIK_LOCAL_RULE_TABLE must be raw or filter")
        ca_file = os.environ.get("MIKROTIK_CA_FILE")
        cfg["ssl_context"] = ssl.create_default_context(cafile=ca_file or None)
    return cfg


def _get_json(
    cfg: dict[str, Any], path: str, params: dict[str, str] | None = None
) -> object:
    query = urllib.parse.urlencode(params or {})
    url = f"{cfg['url']}/{path}" + (f"?{query}" if query else "")
    credentials = base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
    request = urllib.request.Request(
        url, headers={"Authorization": f"Basic {credentials}"}
    )
    with urllib.request.urlopen(
        request, timeout=15, context=cfg["ssl_context"]
    ) as response:
        return json.load(response)


def _rows(value: object, description: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"Unexpected RouterOS {description} response")
    return cast("list[dict[str, Any]]", value)


def _nonnegative(value: object, description: str) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise ValueError(f"Invalid RouterOS {description} counter")
    return int(value)


def _uptime_seconds(value: object) -> int:
    if not isinstance(value, str):
        raise TypeError("Invalid RouterOS uptime")
    colon = re.fullmatch(r"(?:(\d+)w)?(?:(\d+)d)?(\d+):(\d{2}):(\d{2})", value)
    if colon:
        weeks, days, hours, minutes, seconds = (
            int(part or 0) for part in colon.groups()
        )
        if minutes >= 60 or seconds >= 60:
            raise ValueError("Invalid RouterOS uptime")
        return weeks * 604800 + days * 86400 + hours * 3600 + minutes * 60 + seconds
    seconds = 0
    units = {"y": 365 * 86400, "w": 7 * 86400, "d": 86400, "h": 3600, "m": 60, "s": 1}
    position = 0
    for match in UPTIME_PART.finditer(value):
        if match.start() != position:
            raise ValueError("Invalid RouterOS uptime")
        seconds += int(match.group(1)) * units[match.group(2)]
        position = match.end()
    if position != len(value) or not value:
        raise ValueError("Invalid RouterOS uptime")
    return seconds


def _fetch_snapshot(cfg: dict[str, Any]) -> _Snapshot:
    # Request only the two rule tables, two address lists, and router uptime.
    rule_rows = {}
    for table in ("raw", "filter"):
        rule_rows[table] = _rows(
            _get_json(
                cfg,
                f"ip/firewall/{table}",
                {"action": "drop", ".proplist": RULE_FIELDS},
            ),
            f"{table} rules",
        )
    counters = {}
    local_count = 0
    for table, rules in rule_rows.items():
        for rule in rules:
            if (
                rule.get("action") != "drop"
                or rule.get("disabled") == "true"
                or rule.get("invalid") == "true"
            ):
                continue
            comment = rule.get("comment") or ""
            source = None
            if (
                table == cfg["local_table"]
                and comment == cfg["local_comment"]
                and rule.get("src-address-list") == cfg["local_list"]
            ):
                source = "local"
                local_count += 1
            elif (
                cfg["crowdsec_signature"] in comment
                and rule.get("src-address-list") == cfg["crowdsec_list"]
            ):
                source = "crowdsec"
            if source is None:
                continue
            rule_id = rule.get(".id")
            if not rule_id:
                raise ValueError("Matched RouterOS rule has no .id")
            key = f"{source}:{table}:{rule_id}"
            counters[key] = {
                metric: _nonnegative(rule.get(metric), metric) for metric in METRICS
            }
    if local_count != 1:
        raise ValueError(f"Expected exactly one local drop rule, found {local_count}")

    sizes = {}
    for source, list_name in (
        ("local", cfg["local_list"]),
        ("crowdsec", cfg["crowdsec_list"]),
    ):
        sizes[source] = len(
            _rows(
                _get_json(
                    cfg,
                    "ip/firewall/address-list",
                    {"list": list_name, ".proplist": ".id"},
                ),
                f"{source} address list",
            )
        )
    resource = _get_json(cfg, "system/resource", {".proplist": "uptime"})
    if not isinstance(resource, dict):
        raise TypeError("Unexpected RouterOS resource response")
    return {
        "counters": counters,
        "sizes": sizes,
        "uptime": _uptime_seconds(resource.get("uptime")),
    }


def _week_start(now: datetime, tz: ZoneInfo) -> str:
    local = now.astimezone(tz).date()
    return (local - timedelta(days=local.weekday())).isoformat()


def _empty_period(start: str) -> _Period:
    return {
        "start": start,
        "totals": {source: {metric: 0 for metric in METRICS} for source in SOURCES},
        "max_sizes": {source: 0 for source in SOURCES},
        "last_sizes": {source: 0 for source in SOURCES},
        "samples": 0,
        "router_reboots": 0,
        "counter_resets": 0,
        "rule_rebaselines": 0,
    }


def _initial_state(start: str) -> _State:
    return {
        "version": 1,
        "period": _empty_period(start),
        "pending": [],
        "counters": {},
        "last_sample_at": None,
        "last_uptime": None,
    }


def _open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = sqlite3.connect(path, timeout=30)
    os.chmod(path, 0o600)
    database.row_factory = sqlite3.Row
    database.executescript("""
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
    """)
    return database


def _period_from_row(row: sqlite3.Row) -> _Period:
    period = _empty_period(row["start"])
    for source in SOURCES:
        for metric in METRICS:
            period["totals"][source][metric] = row[f"{source}_{metric}"]
        period["max_sizes"][source] = row[f"{source}_max_size"]
        period["last_sizes"][source] = row[f"{source}_last_size"]
    for field in ("samples", "router_reboots", "counter_resets", "rule_rebaselines"):
        period[field] = row[field]
    return period


def _load_state(database: sqlite3.Connection, start: str) -> _State:
    meta = database.execute("SELECT * FROM metadata WHERE id = 1").fetchone()
    if meta is None:
        return _initial_state(start)
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


def _roll_period(state: _State, start: str) -> None:
    current = state["period"]["start"]
    while current < start:
        state["pending"].append(state["period"])
        current = (
            datetime.fromisoformat(current).date() + timedelta(days=7)
        ).isoformat()
        state["period"] = _empty_period(current)
    if current > start:
        raise ValueError("Clock moved into an earlier reporting week")


def _apply_snapshot(
    state: _State, snapshot: _Snapshot, now: datetime, tz: ZoneInfo
) -> None:
    _roll_period(state, _week_start(now, tz))
    period = state["period"]
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
        period["router_reboots"] += 1
    for key, current in snapshot["counters"].items():
        previous = state["counters"].get(key)
        if previous_at is None or previous is None:
            if previous_at is not None:
                period["rule_rebaselines"] += 1
            continue  # A new rule starts with a conservative baseline.
        source = key.split(":", 1)[0]
        reset = reboot or any(current[metric] < previous[metric] for metric in METRICS)
        if reset and not reboot:
            period["counter_resets"] += 1
        for metric in METRICS:
            period["totals"][source][metric] += (
                current[metric] if reset else current[metric] - previous[metric]
            )
    state["counters"] = snapshot["counters"]
    for source in SOURCES:
        size = snapshot["sizes"][source]
        period["last_sizes"][source] = size
        period["max_sizes"][source] = max(period["max_sizes"][source], size)
    period["samples"] += 1
    state["last_sample_at"] = now.isoformat()
    state["last_uptime"] = snapshot["uptime"]


def _save_state(database: sqlite3.Connection, state: _State) -> None:
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
    for status, period in (("pending", item) for item in state["pending"]):
        _insert_period(database, status, period)
    _insert_period(database, "active", state["period"])


def _insert_period(database: sqlite3.Connection, status: str, period: _Period) -> None:
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


def _render_report(period: _Period, tz: ZoneInfo) -> str:
    start = datetime.fromisoformat(period["start"]).date()
    end = start + timedelta(days=7)
    lines = [
        f"MikroTik blocking report: {start} to {end} ({tz.key}, end exclusive)",
        f"Collector samples: {period['samples']}",
        "",
        "Local MikroTik detection (wan-scanners or configured local list)",
        f"  Packets dropped: {period['totals']['local']['packets']:,}",
        f"  Bytes dropped: {period['totals']['local']['bytes']:,}",
        f"  Address list size, latest / observed maximum: {period['last_sizes']['local']} / {period['max_sizes']['local']}",
        "",
        "CrowdSec decisions enforced by RouterOS bouncer",
        f"  Packets dropped: {period['totals']['crowdsec']['packets']:,}",
        f"  Bytes dropped: {period['totals']['crowdsec']['bytes']:,}",
        f"  Address list size, latest / observed maximum: {period['last_sizes']['crowdsec']} / {period['max_sizes']['crowdsec']}",
        "",
        f"Detected router reboots: {period['router_reboots']}",
        f"Other counter resets: {period['counter_resets']}",
        f"New or recreated rule baselines: {period['rule_rebaselines']}",
        "",
        "Counters measure packets and bytes discarded by the selected rules, not unique IPs or attacks.",
        "CrowdSec remains responsible for detecting and classifying attacks.",
    ]
    if period["samples"] == 0:
        lines.extend(
            (
                "",
                "No collector samples were recorded; zero totals do not mean zero blocked traffic.",
            )
        )
    return "\n".join(lines) + "\n"


def _send_report(cfg: dict[str, Any], period: _Period) -> None:
    command = [
        str(cfg["notifier"]),
        "--to",
        cfg["recipient"],
        "--subject",
        f"{cfg['subject']} ({period['start']})",
    ]
    if cfg["account"]:
        command.extend(("--account", cfg["account"]))
    if cfg["from"]:
        command.extend(("--from", cfg["from"]))
    subprocess.run(
        command, input=_render_report(period, cfg["timezone"]), text=True, check=True
    )


def _process_report(
    database: sqlite3.Connection, cfg: dict[str, Any], now: datetime
) -> None:
    database.execute("BEGIN IMMEDIATE")
    state = _load_state(database, _week_start(now, cfg["timezone"]))
    _roll_period(state, _week_start(now, cfg["timezone"]))
    _save_state(database, state)
    database.commit()
    while state["pending"]:
        database.execute("BEGIN IMMEDIATE")
        # Reload after waiting for any concurrent collector.
        state = _load_state(database, _week_start(now, cfg["timezone"]))
        if not state["pending"]:
            database.commit()
            break
        period = state["pending"][0]
        _send_report(cfg, period)
        state["pending"].pop(0)
        _save_state(database, state)
        database.commit()
        print(f"Sent report for week {period['start']}")


def _collect(cfg: dict[str, Any], now: datetime) -> None:
    snapshot = _fetch_snapshot(cfg)
    with closing(_open_database(cfg["state"])) as database:
        database.execute("BEGIN IMMEDIATE")
        state = _load_state(database, _week_start(now, cfg["timezone"]))
        _apply_snapshot(state, snapshot, now, cfg["timezone"])
        _save_state(database, state)
        database.commit()
        print(
            f"Collected {len(snapshot['counters'])} rules for week {state['period']['start']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "report"))
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    if args.command == "collect":
        _collect(_config(), now)
    else:
        cfg = _config(report=True)
        with closing(_open_database(cfg["state"])) as database:
            _process_report(database, cfg, now)


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        TypeError,
        OSError,
        sqlite3.Error,
        urllib.error.URLError,
        subprocess.CalledProcessError,
        KeyError,
        json.JSONDecodeError,
    ) as error:
        print(f"mikrotik-report: {error}", file=sys.stderr)
        sys.exit(1)
