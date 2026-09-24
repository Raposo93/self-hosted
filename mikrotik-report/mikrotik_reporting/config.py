"""Load and validate command-specific environment configuration."""

from __future__ import annotations

import os
import ssl
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class CommonConfig:
    state: Path
    timezone: ZoneInfo


@dataclass(frozen=True)
class RouterOSConfig:
    url: str
    user: str
    password: str
    local_list: str
    local_comment: str
    local_table: str
    crowdsec_list: str
    crowdsec_signature: str
    detection_log_buffer: str
    detection_log_prefix: str
    ssl_context: ssl.SSLContext


@dataclass(frozen=True)
class MailConfig:
    recipient: str
    subject: str
    notifier: Path
    account: str
    sender: str


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be set")
    return value


def _defaulted(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _required_absolute_path(name: str) -> Path:
    path = Path(_required(name))
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def load_common_config() -> CommonConfig:
    state = Path(_required("MIKROTIK_REPORT_DB"))
    if not state.is_absolute():
        raise ValueError("MIKROTIK_REPORT_DB must be an absolute path")
    return CommonConfig(
        state=state,
        timezone=ZoneInfo(os.environ.get("MIKROTIK_REPORT_TIMEZONE", "UTC")),
    )


def load_routeros_config() -> RouterOSConfig:
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
    local_table = os.environ.get("MIKROTIK_LOCAL_RULE_TABLE", "raw")
    if local_table not in ("raw", "filter"):
        raise ValueError("MIKROTIK_LOCAL_RULE_TABLE must be raw or filter")
    ca_file = os.environ.get("MIKROTIK_CA_FILE")
    return RouterOSConfig(
        url=url,
        user=_required("MIKROTIK_USER"),
        password=_required("MIKROTIK_PASSWORD"),
        local_list=_required("MIKROTIK_LOCAL_LIST"),
        local_comment=_required("MIKROTIK_LOCAL_RULE_COMMENT"),
        local_table=local_table,
        crowdsec_list=_required("MIKROTIK_CROWDSEC_LIST"),
        crowdsec_signature=_required("MIKROTIK_CROWDSEC_RULE_SIGNATURE"),
        detection_log_buffer=_defaulted(
            "MIKROTIK_DETECTION_LOG_BUFFER", "mikrotik-report"
        ),
        detection_log_prefix=_defaulted(
            "MIKROTIK_DETECTION_LOG_PREFIX", "mikrotik-report-detect"
        ),
        ssl_context=ssl.create_default_context(cafile=ca_file or None),
    )


def load_mail_config(*, monthly: bool = False) -> MailConfig:
    return MailConfig(
        recipient=_required("MIKROTIK_REPORT_TO"),
        subject=os.environ.get(
            "MIKROTIK_MONTHLY_SUBJECT" if monthly else "MIKROTIK_REPORT_SUBJECT",
            "MikroTik monthly blocking report"
            if monthly
            else "MikroTik weekly blocking report",
        ),
        notifier=_required_absolute_path("MIKROTIK_REPORT_NOTIFIER"),
        account=os.environ.get("MIKROTIK_REPORT_MAIL_ACCOUNT", ""),
        sender=os.environ.get("MIKROTIK_REPORT_FROM", ""),
    )
