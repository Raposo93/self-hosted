from __future__ import annotations

import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoneinfo import ZoneInfo

from mikrotik_reporting.config import CommonConfig, MailConfig, RouterOSConfig
from mikrotik_reporting.models import Snapshot

UTC = ZoneInfo("UTC")


def sample(
    packets: int,
    bytes_: int,
    uptime: int = 100,
    rule_id: str = "*1",
    local_size: int = 2,
    crowd_size: int = 3,
) -> Snapshot:
    return {
        "counters": {
            f"local:raw:{rule_id}": {"packets": packets, "bytes": bytes_},
            "crowdsec:filter:*2": {"packets": 10, "bytes": 1000},
        },
        "sizes": {"local": local_size, "crowdsec": crowd_size},
        "uptime": uptime,
    }


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


def common(path: Path, timezone_: ZoneInfo = UTC) -> CommonConfig:
    return CommonConfig(state=path, timezone=timezone_)


def router() -> RouterOSConfig:
    return RouterOSConfig(
        url="https://router.example.net/rest",
        user="report-reader",
        password="secret",
        local_list="wan-scanners",
        local_comment="Drop WAN scanners",
        local_table="raw",
        crowdsec_list="crowdsec-banned",
        crowdsec_signature="@cs-routeros-bouncer",
        ssl_context=ssl.create_default_context(),
    )


def mail(directory: Path) -> MailConfig:
    return MailConfig(
        recipient="test@example.net",
        subject="MikroTik report",
        notifier=directory / "send-mail.sh",
        account="test-account",
        sender="",
    )
