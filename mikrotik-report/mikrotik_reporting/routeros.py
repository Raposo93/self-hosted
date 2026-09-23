"""Read and validate the RouterOS counters used by the collector."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from .config import RouterOSConfig
from .models import METRICS, DetectionBatch, DetectionEvent, Snapshot

RULE_FIELDS = ".id,comment,action,src-address-list,packets,bytes,disabled,invalid"
UPTIME_PART = re.compile(r"(\d+)([ywdhms])")
LOG_TIME = re.compile(
    r"(?:(?P<month>[a-z]{3})/(?P<day>\d{1,2})(?:/(?P<year>\d{4}))? )?"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(?:\.(?P<fraction>\d+))?",
    re.IGNORECASE,
)
ISO_LOG_TIME = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2}) "
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
)
PORT_PROTOCOL = re.compile(r"\bproto (?P<protocol>TCP|UDP)\b", re.IGNORECASE)
IPV4_PORTS = re.compile(
    r"(?P<source>(?:\d{1,3}\.){3}\d{1,3}):\d+->"
    r"(?:\d{1,3}\.){3}\d{1,3}:(?P<destination_port>\d+)\b"
)
MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ),
        start=1,
    )
}


def _get_json(
    config: RouterOSConfig, path: str, params: dict[str, str] | None = None
) -> object:
    query = urllib.parse.urlencode(params or {})
    url = f"{config.url}/{path}" + (f"?{query}" if query else "")
    credentials = base64.b64encode(f"{config.user}:{config.password}".encode()).decode()
    request = urllib.request.Request(
        url, headers={"Authorization": f"Basic {credentials}"}
    )
    with urllib.request.urlopen(
        request, timeout=15, context=config.ssl_context
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


def uptime_seconds(value: object) -> int:
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
    units = {
        "y": 365 * 86400,
        "w": 7 * 86400,
        "d": 86400,
        "h": 3600,
        "m": 60,
        "s": 1,
    }
    position = 0
    for match in UPTIME_PART.finditer(value):
        if match.start() != position:
            raise ValueError("Invalid RouterOS uptime")
        seconds += int(match.group(1)) * units[match.group(2)]
        position = match.end()
    if position != len(value) or not value:
        raise ValueError("Invalid RouterOS uptime")
    return seconds


def _microseconds(fraction: str | None) -> int:
    return int((fraction or "").ljust(6, "0")[:6] or 0)


def log_datetime(value: object, reference: datetime, timezone_: ZoneInfo) -> datetime:
    if not isinstance(value, str):
        raise TypeError("Invalid RouterOS log time")
    local_reference = reference.astimezone(timezone_)
    iso = ISO_LOG_TIME.fullmatch(value)
    if iso:
        parts = iso.groupdict()
        return datetime(
            int(parts["year"]),
            int(parts["month"]),
            int(parts["day"]),
            int(parts["hour"]),
            int(parts["minute"]),
            int(parts["second"]),
            _microseconds(parts["fraction"]),
            timezone_,
        )
    match = LOG_TIME.fullmatch(value)
    if not match:
        raise ValueError("Invalid RouterOS log time")
    parts = match.groupdict()
    month_name = parts["month"]
    if month_name is None:
        year = local_reference.year
        month = local_reference.month
        day = local_reference.day
    else:
        month = MONTHS.get(month_name.lower(), 0)
        if not month:
            raise ValueError("Invalid RouterOS log time")
        year = int(parts["year"] or local_reference.year)
        day = int(parts["day"])
    parsed = datetime(
        year,
        month,
        day,
        int(parts["hour"]),
        int(parts["minute"]),
        int(parts["second"]),
        _microseconds(parts["fraction"]),
        timezone_,
    )
    if (
        month_name is not None
        and parts["year"] is None
        and parsed > local_reference + timedelta(days=1)
    ):
        parsed = parsed.replace(year=parsed.year - 1)
    return parsed


def _detection_event(
    row: dict[str, Any],
    config: RouterOSConfig,
    reference: datetime,
    timezone_: ZoneInfo,
) -> DetectionEvent | None:
    message = row.get("message")
    if not isinstance(message, str) or not message.startswith(
        config.detection_log_prefix
    ):
        return None
    protocol_match = PORT_PROTOCOL.search(message)
    if protocol_match is None:
        return None
    ports = IPV4_PORTS.search(message)
    if ports is None:
        raise ValueError("Invalid RouterOS detection log message")
    source = str(ipaddress.IPv4Address(ports.group("source")))
    destination_port = int(ports.group("destination_port"))
    if destination_port > 65535:
        raise ValueError("Invalid RouterOS detection destination port")
    entry_id = row.get(".id")
    entry_time = row.get("time")
    if not isinstance(entry_id, str) or not entry_id:
        raise ValueError("RouterOS detection log entry has no .id")
    occurred_at = log_datetime(entry_time, reference, timezone_)
    fingerprint = hashlib.sha256(
        f"{entry_id}\0{entry_time}\0{message}".encode()
    ).hexdigest()
    return {
        "fingerprint": fingerprint,
        "day": occurred_at.date().isoformat(),
        "source_ip": source,
        "protocol": protocol_match.group("protocol").lower(),
        "destination_port": destination_port,
    }


def fetch_detection_batch(
    config: RouterOSConfig, reference: datetime, timezone_: ZoneInfo
) -> DetectionBatch:
    rows = _rows(
        _get_json(
            config,
            "log",
            {
                "buffer": config.detection_log_buffer,
                ".proplist": ".id,time,topics,message",
            },
        ),
        "detection log",
    )
    fingerprints = []
    events = []
    seen = set()
    for row in rows:
        message = row.get("message")
        if not isinstance(message, str) or not message.startswith(
            config.detection_log_prefix
        ):
            continue
        entry_id = row.get(".id")
        entry_time = row.get("time")
        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError("RouterOS detection log entry has no .id")
        fingerprint = hashlib.sha256(
            f"{entry_id}\0{entry_time}\0{message}".encode()
        ).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        fingerprints.append(fingerprint)
        event = _detection_event(row, config, reference, timezone_)
        if event is not None:
            events.append(event)
    return {"fingerprints": fingerprints, "events": events}


def fetch_snapshot(config: RouterOSConfig) -> Snapshot:
    # Request only the two rule tables, two address lists, and router uptime.
    rule_rows = {}
    for table in ("raw", "filter"):
        rule_rows[table] = _rows(
            _get_json(
                config,
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
                table == config.local_table
                and comment == config.local_comment
                and rule.get("src-address-list") == config.local_list
            ):
                source = "local"
                local_count += 1
            elif (
                config.crowdsec_signature in comment
                and rule.get("src-address-list") == config.crowdsec_list
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
        ("local", config.local_list),
        ("crowdsec", config.crowdsec_list),
    ):
        sizes[source] = len(
            _rows(
                _get_json(
                    config,
                    "ip/firewall/address-list",
                    {"list": list_name, ".proplist": ".id"},
                ),
                f"{source} address list",
            )
        )
    resource = _get_json(config, "system/resource", {".proplist": "uptime"})
    if not isinstance(resource, dict):
        raise TypeError("Unexpected RouterOS resource response")
    return {
        "counters": counters,
        "sizes": sizes,
        "uptime": uptime_seconds(resource.get("uptime")),
    }
