"""Check filesystem capacity/inodes and diagnose new or worsening alerts."""

import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from config import integer
from notification import load, save, send

# Explicitly include disk and common persistent network filesystems; exclude
# virtual, memory, container overlay and automounter filesystems.
FILESYSTEMS = {
    "ext2",
    "ext3",
    "ext4",
    "xfs",
    "btrfs",
    "zfs",
    "vfat",
    "exfat",
    "ntfs",
    "ntfs3",
    "fuseblk",
    "nfs",
    "nfs4",
    "cifs",
    "fuse.sshfs",
}


def unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def mounts(text: str) -> list[tuple[str, str, str]]:
    candidates = []
    for line in text.splitlines():
        left, right = line.split(" - ", 1)
        fields, filesystem = left.split(), right.split()
        if filesystem[0] in FILESYSTEMS:
            candidates.append((fields[2], unescape(fields[4]), unescape(filesystem[1])))
    # One check per device: prefer the topmost mount, avoiding bind aliases.
    result = {}
    for device, mount, source in sorted(candidates, key=lambda item: len(item[1])):
        result.setdefault(device, (device, mount, source))
    return list(result.values())


def usage(path: str) -> dict[str, Any]:
    stats = os.statvfs(path)
    used = stats.f_blocks - stats.f_bfree
    available = stats.f_bavail
    total = used + available
    percent = math.ceil(100 * used / total) if total else 0
    inodes = (
        math.ceil(100 * (stats.f_files - stats.f_ffree) / stats.f_files)
        if stats.f_files
        else 0
    )
    return {
        "capacity": percent,
        "inodes": inodes,
        "used_bytes": used * stats.f_frsize,
        "available_bytes": available * stats.f_frsize,
        "inode_total": stats.f_files,
        "inode_free": stats.f_ffree,
    }


def status(values: dict[str, Any], warning: int, critical: int) -> str:
    highest = max(values["capacity"], values["inodes"])
    return (
        "CRITICAL" if highest >= critical else "WARNING" if highest >= warning else "OK"
    )


def changed(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    return (
        previous.get("status") != current["status"]
        or current["capacity"] > previous.get("capacity", 0)
        or current["inodes"] > previous.get("inodes", 0)
    )


def diagnose(
    mount: str, used_bytes: int, depth: int, timeout: int
) -> tuple[list[str], bool]:
    lines = []
    deadline = time.monotonic() + timeout
    current = mount
    for level in range(depth):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return [
                *lines,
                "Diagnostic budget exhausted; results are incomplete",
            ], False
        # File-backed output avoids keeping an unbounded du result in memory.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                completed = subprocess.run(
                    [
                        "du",
                        "-x",
                        "--max-depth=1",
                        "--block-size=1",
                        "--null",
                        "--",
                        current,
                    ],
                    stdout=output,
                    stderr=errors,
                    timeout=remaining,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return [*lines, "du timed out; results are incomplete"], False
            if completed.returncode != 0:
                return [
                    *lines,
                    "du failed (permissions, I/O or changing files); results are incomplete",
                ], False
            output.seek(0)
            root_size = None
            largest = (0, "")
            # Read NUL records, preserving whitespace/newlines in path names.
            pending = b""
            while chunk := output.read(65536):
                records = (pending + chunk).split(b"\0")
                pending = records.pop()
                for record in records:
                    size, path = record.split(b"\t", 1)
                    entry = (int(size), os.fsdecode(path))
                    if entry[1] == current:
                        root_size = entry[0]
                    elif entry > largest:
                        largest = entry
        if root_size is None:
            return [*lines, "du returned no total; results are incomplete"], False
        if level == 0:
            lines.append(
                f"du allocated bytes under mount: {root_size}; filesystem used bytes: {used_bytes}"
            )
            if abs(root_size - used_bytes) > max(1024**3, used_bytes // 10):
                lines.append(
                    "Filesystem and du totals differ materially. Deleted-open files, metadata, snapshots, hidden mount contents or subvolumes may explain it; this directory scan does not account for all usage."
                )
        if largest[0] == 0:
            lines.append(
                f"No nonempty child directories under {current!r}; files may be directly in this directory."
            )
            break
        size, current = largest
        lines.append(f"Level {level + 1}: {current!r}: {size} allocated bytes")
    return lines, True


def main() -> int:
    failed = False
    try:
        warning = integer("DISK_WARNING", 85, 1, 99)
        critical = integer("DISK_CRITICAL", 95, 2, 100)
        depth = integer("DISK_MAX_DEPTH", 3, 1, 10)
        timeout = integer("DISK_DIAG_TIMEOUT", 60, 1, 600)
        previous = load("disk-space")
        for entry in previous.values():
            if not isinstance(entry, dict):
                raise TypeError("Invalid disk notification state")
            if entry and (
                entry.get("status") not in {"OK", "WARNING", "CRITICAL"}
                or any(
                    not isinstance(entry.get(name), int)
                    for name in ("capacity", "inodes")
                )
            ):
                raise ValueError("Invalid disk notification state")
        current_state = {}
        for device, mount, source in mounts(Path("/proc/self/mountinfo").read_text()):
            key = f"{device}:{source}:{mount}"
            try:
                values = usage(mount)
                values["status"] = status(values, warning, critical)
                old = previous.get(key, {})
                if values["status"] == "OK":
                    current_state[key] = values
                    continue
                if not changed(old, values):
                    # Keep last notified percentages so gradual worsening is visible.
                    current_state[key] = old
                    continue
                diagnosis, complete = diagnose(
                    mount, values["used_bytes"], depth, timeout
                )
                body = "\n".join(
                    [
                        f"Host: {os.environ['HOST_NAME']}",
                        f"Filesystem: {source!r}; mount: {mount!r}",
                        f"State: {values['status']}",
                        f"Capacity used: {values['capacity']}%; free: {100 - values['capacity']}% (df usable-space basis)",
                        f"Available bytes: {values['available_bytes']}",
                        f"Inodes used: {values['inodes']}%; free: {values['inode_free']}/{values['inode_total']} (0 total means unsupported)",
                        "Directory sizes diagnose allocated bytes, not inode consumers.",
                        *diagnosis,
                    ]
                )
                print(body, flush=True)
                send(f"DISK {values['status']}", body)
                current_state[key] = values if complete else old
                failed |= not complete
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                print(
                    f"Disk check failed for mount {mount!r}; inspect filesystem, state and notifier",
                    file=sys.stderr,
                )
                current_state[key] = previous.get(key, {})
                failed = True
        save("disk-space", current_state)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print(
            "Disk check failed; inspect configuration, mount table and state",
            file=sys.stderr,
        )
        return 1
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
