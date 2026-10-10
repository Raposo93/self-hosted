"""Reuse the established APT/reboot detector with change-only notifications."""

import importlib.util
import sys
from pathlib import Path

from notification import load, save, send

spec = importlib.util.spec_from_file_location(
    "security_update_check",
    Path(__file__).resolve().parents[2]
    / "security-update-check/security_update_check.py",
)
assert spec and spec.loader
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


def notify(state: str, body: str, hostname: str) -> None:
    # The full sorted package/version and reboot report is the fingerprint.
    previous = load("security-updates")
    if previous and not isinstance(previous.get("body"), str):
        raise ValueError("Invalid security notification state")
    if state == "OK":
        if previous:
            save("security-updates", {})
        return
    if previous.get("body") != body:
        send(state, body)
        save("security-updates", {"body": body})


if __name__ == "__main__":
    sys.exit(engine.main(notification=notify))
