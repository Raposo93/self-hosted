"""Read and validate the RouterOS counters used by the collector."""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
import urllib.request
from typing import Any, Optional, cast

from .config import RouterOSConfig
from .models import METRICS, Snapshot

RULE_FIELDS = ".id,comment,action,src-address-list,packets,bytes,disabled,invalid"
UPTIME_PART = re.compile(r"(\d+)([ywdhms])")


def _get_json(
    config: RouterOSConfig, path: str, params: Optional[dict[str, str]] = None
) -> object:
    query = urllib.parse.urlencode(params or {})
    url = f"{config.url}/{path}" + (f"?{query}" if query else "")
    credentials = base64.b64encode(
        f"{config.user}:{config.password}".encode()
    ).decode()
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
