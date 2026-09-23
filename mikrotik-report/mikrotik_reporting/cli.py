"""Command-line interface for the MikroTik blocking report."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone

from .config import load_common_config, load_mail_config, load_routeros_config
from .workflows import collect, send_monthly_reports, send_preview, send_weekly_reports

DESCRIPTION = (
    "Collect RouterOS drop counters and mail completed weekly or monthly summaries."
)


def main() -> None:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "command", choices=("collect", "report", "report-monthly", "test-report")
    )
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    common = load_common_config()
    if args.command == "collect":
        collect(common, load_routeros_config(), now)
    elif args.command == "report":
        send_weekly_reports(common, load_mail_config(), now)
    elif args.command == "report-monthly":
        send_monthly_reports(common, load_mail_config(monthly=True), now)
    else:
        send_preview(common, load_routeros_config(), load_mail_config(), now)


def run() -> None:
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
