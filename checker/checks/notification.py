"""Small shared mail/state helper; callers decide when attention changed."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any


def load(name: str) -> dict[str, Any]:
    path = Path(os.environ["CHECKER_STATE_DIR"]) / f"{name}.json"
    try:
        result = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(result, dict):
        raise TypeError("Invalid notification state")
    return result


def save(name: str, state: dict[str, Any]) -> None:
    path = Path(os.environ["CHECKER_STATE_DIR"]) / f"{name}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.chmod(0o600)
    temporary.replace(path)


def send(subject: str, body: str) -> None:
    command = [
        os.environ["SEND_MAIL"],
        "--to",
        os.environ["RECIPIENT_EMAIL"],
        "--subject",
        f"[{os.environ['HOST_NAME']}] {subject}",
    ]
    for name, flag in (("MSMTP_ACCOUNT", "--account"), ("SENDER_EMAIL", "--from")):
        if os.environ.get(name):
            command.extend([flag, os.environ[name]])
    subprocess.run(command, input=body, text=True, check=True, timeout=120)
