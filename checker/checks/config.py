"""Validate shared configuration before any checks or APT writes."""

import os
import sys
from pathlib import Path


def integer(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def validate() -> None:
    for name in ("CHECK_SECURITY_UPDATES", "CHECK_DISK_SPACE"):
        if os.environ.get(name) not in {"true", "false"}:
            raise ValueError(f"{name} must be true or false")
    warning = integer("DISK_WARNING", 85, 1, 99)
    critical = integer("DISK_CRITICAL", 95, 2, 100)
    if warning >= critical:
        raise ValueError("DISK_WARNING must be below DISK_CRITICAL")
    integer("DISK_MAX_DEPTH", 3, 1, 10)
    integer("DISK_DIAG_TIMEOUT", 60, 1, 600)
    for name in ("HOST_NAME", "RECIPIENT_EMAIL"):
        value = os.environ.get(name, "")
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError(f"{name} is required and must be a single line")
    for name in ("SEND_MAIL", "CHECKER_STATE_DIR"):
        if not Path(os.environ.get(name, "")).is_absolute():
            raise ValueError(f"{name} must be an absolute path")
    notifier = Path(os.environ["SEND_MAIL"])
    if not notifier.is_file() or not os.access(notifier, os.X_OK):
        raise ValueError("SEND_MAIL must be an executable file")


if __name__ == "__main__":
    try:
        validate()
    except (ValueError, OSError):
        print("Invalid checker configuration; review .env and README", file=sys.stderr)
        sys.exit(1)
