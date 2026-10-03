"""Verify a PBS sentinel restore without retaining restored data."""

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


class CheckError(ValueError):
    """A safe diagnostic that contains no client output or credentials."""


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise CheckError(f"Missing {name}")
    return value


def client(arguments: list[str]) -> str:
    environment = os.environ.copy()
    # Namespace selection is controlled by NAMESPACE, including an empty root.
    environment.pop("PBS_NAMESPACE", None)
    result = subprocess.run(
        ["proxmox-backup-client", *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("RESTORE_TIMEOUT_SECONDS", "3600")),
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode:
        # Client errors may contain authentication details; never forward them.
        raise CheckError(f"PBS {arguments[0]} failed (exit {result.returncode})")
    return result.stdout


def latest_snapshot(raw: str, group: str, max_age: int, now: float) -> str:
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise TypeError("Invalid snapshot list")
    timestamps = []
    for row in rows:
        if f"{row['backup-type']}/{row['backup-id']}" != group:
            continue
        timestamp = row["backup-time"]
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            raise TypeError("Invalid snapshot timestamp")
        timestamps.append(timestamp)
    if not timestamps:
        raise CheckError("No snapshots found for configured group")
    newest = max(timestamps)
    if newest > now + 300:
        raise CheckError("Snapshot timestamp is in the future")
    if max_age and now - newest > max_age:
        raise CheckError("Latest snapshot exceeds maximum age")
    stamp = datetime.fromtimestamp(newest, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{group}/{stamp}"


def verify() -> str:
    repository = required("REPO")
    group = required("RESTORE_GROUP")
    archive = required("BACKUP_NAME")
    namespace = os.environ.get("NAMESPACE", "")
    if not re.fullmatch(r"host/[A-Za-z0-9][A-Za-z0-9_.-]*", group):
        raise ValueError("RESTORE_GROUP must be host/<backup-id>")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.pxar", archive):
        raise ValueError("BACKUP_NAME must be a pxar archive")
    sentinel = os.environ.get("SENTINEL_PATH", ".pbc-restore-sentinel")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", sentinel) or sentinel in {".", ".."}:
        raise ValueError("SENTINEL_PATH must be a literal filename at the archive root")
    max_age = int(os.environ.get("MAX_SNAPSHOT_AGE_SECONDS", "0"))
    if max_age < 0 or int(os.environ.get("RESTORE_TIMEOUT_SECONDS", "3600")) <= 0:
        raise ValueError("Invalid age or timeout")
    checksum = os.environ.get("SENTINEL_SHA256", "")
    if checksum and not re.fullmatch(r"[a-fA-F0-9]{64}", checksum):
        raise ValueError("Invalid SENTINEL_SHA256")
    key = os.environ.get("ENCRYPTION_KEYFILE", "")
    credential = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/nonexistent")) / (
        "proxmox-backup-client.encryption-password"
    )
    if bool(key) != (
        credential.is_file() or bool(os.environ.get("PBS_ENCRYPTION_PASSWORD"))
    ):
        raise ValueError("Incomplete encryption configuration")
    if key and not Path(key).is_file():
        raise ValueError("Encryption key is unavailable")
    common = ["--repository", repository]
    if namespace:
        common.extend(["--ns", namespace])
    snapshot = latest_snapshot(
        client(["snapshot", "list", group, "--output-format", "json", *common]),
        group,
        max_age,
        time.time(),
    )
    with tempfile.TemporaryDirectory(
        prefix="pbc-restore-check-", dir=required("RESTORE_TMP_BASE")
    ) as temporary:
        target = Path(temporary) / "restore"
        arguments = [
            "restore",
            snapshot,
            archive,
            str(target),
            "--pattern",
            f"/{sentinel}",
            *common,
        ]
        if key:
            arguments.extend(["--keyfile", key])
        client(arguments)
        restored = target / sentinel
        if restored.is_symlink() or not restored.is_file():
            raise CheckError("Sentinel missing or not a regular file")
        expected = (
            os.environ.get("SENTINEL_CONTENT", "pbc-restore-sentinel-v1") + "\n"
        ).encode()
        with restored.open("rb") as stream:
            content = stream.read(len(expected) + 1)
        if content != expected:
            raise CheckError("Sentinel content mismatch")
        if checksum and hashlib.sha256(content).hexdigest() != checksum.lower():
            raise CheckError("Sentinel checksum mismatch")
    selected_namespace = namespace or "<root>"
    return (
        f"Restored and validated {sentinel} from {snapshot} "
        f"({archive}, namespace {selected_namespace})."
    )


def interrupted(_signum: int, _frame: object) -> None:
    raise InterruptedError("Restore verification interrupted")


def main() -> int:
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    status = 0
    try:
        message = verify()
    except CheckError as error:
        message = f"Restore verification failed: {error}"
        status = 1
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        OverflowError,
        subprocess.SubprocessError,
    ):
        # Do not include arbitrary exception text: it can contain secrets.
        message = "Restore verification failed; check profile, PBS access, snapshot age and sentinel."
        status = 1
    print(message, flush=True)
    try:
        subject = "❌ Restore verification failed" if status else "✅ Restore verified"
        notifier = Path(__file__).resolve().parent.parent / "mail-notifier/send-mail.sh"
        result = subprocess.run(
            [
                str(notifier),
                "--account",
                required("MSMTP_ACCOUNT"),
                "--from",
                required("SENDER_EMAIL"),
                "--to",
                required("RECIPIENT_EMAIL"),
                "--subject",
                f"{subject}: {required('RESTORE_GROUP')}",
            ],
            input=message + "\n",
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise ValueError("Notification failed")
    except (ValueError, OSError, subprocess.SubprocessError):
        print("Warning: notification failed", flush=True)
        return status or 2
    return status


if __name__ == "__main__":
    raise SystemExit(main())
