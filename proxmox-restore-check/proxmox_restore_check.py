#!/usr/bin/env python3
"""Sequential, offline guest restore drills on a dedicated standalone PVE node."""

import argparse
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


class Failure(RuntimeError):
    pass


def command(args: list[str], timeout: float = 60, body: str | None = None) -> str:
    # Kill the entire CLI process group before cleanup, including restore workers.
    with subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            output, _ = process.communicate(body, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise Failure(f"{args[0]} timed out") from None
        if process.returncode:
            # CLI stderr can contain private backup/configuration details.
            raise Failure(f"{args[0]} failed (exit {process.returncode})")
        return output


@dataclass
class Guest:
    kind: str
    source_id: int
    name: str
    agent: bool = False
    boot_timeout: int = 180


@dataclass
class Config:
    hostname: str
    pbs_storage: str
    target_storage: str
    temporary_id: int
    mail_to: str
    guests: list[Guest]
    restore_timeout: int = 3600
    poweroff: bool = False
    max_test_vcpus: int | None = None


def load_config(path: Path) -> Config:
    data = json.loads(path.read_text())
    data["guests"] = [Guest(**guest) for guest in data["guests"]]
    config = Config(**data)
    for value in (config.hostname, config.pbs_storage, config.target_storage):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("Invalid host/storage identifier")
    if config.pbs_storage == config.target_storage:
        raise ValueError("Source and target storage must differ")
    if type(config.poweroff) is not bool or type(config.temporary_id) is not int:
        raise ValueError("Invalid poweroff/temporary_id")
    if not 100 <= config.temporary_id <= 999999999 or not config.guests:
        raise ValueError("Temporary ID or guest list invalid")
    if type(config.restore_timeout) is not int or config.restore_timeout <= 0:
        raise ValueError("Invalid restore timeout")
    if config.max_test_vcpus is not None and (
        type(config.max_test_vcpus) is not int or config.max_test_vcpus <= 0
    ):
        raise ValueError("Invalid max_test_vcpus")
    if not config.mail_to or any(c in config.mail_to for c in "\r\n"):
        raise ValueError("Invalid mail recipient")
    for guest in config.guests:
        if guest.kind not in ("vm", "ct") or type(guest.source_id) is not int:
            raise ValueError("Invalid guest type/ID")
        if (
            not 100 <= guest.source_id <= 999999999
            or guest.source_id == config.temporary_id
        ):
            raise ValueError("Source and temporary ID must be distinct valid IDs")
        if (
            type(guest.agent) is not bool
            or type(guest.boot_timeout) is not int
            or guest.boot_timeout <= 0
        ):
            raise ValueError("Invalid guest check/timeout")
    return config


def latest_backup(output: str, storage: str, guest: Guest) -> str:
    prefix = f"{storage}:backup/{guest.kind}/{guest.source_id}/"
    volumes = [
        row["volid"]
        for row in json.loads(output)
        if isinstance(row.get("volid"), str)
        and row["volid"].startswith(prefix)
        and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", row["volid"][len(prefix) :]
        )
    ]
    if not volumes:
        raise Failure("No matching PBS backup")
    return max(volumes)


def sanitize(
    text: str, kind: str, storage: str, temporary_id: int = 999
) -> tuple[str, list[str]]:
    """Allow only boot essentials; unknown options fail closed by being removed."""
    vm_keys = {
        "acpi",
        "agent",
        "arch",
        "balloon",
        "bios",
        "boot",
        "cores",
        "cpu",
        "hotplug",
        "machine",
        "memory",
        "name",
        "numa",
        "numa0",
        "ostype",
        "scsihw",
        "smbios1",
        "sockets",
        "vcpus",
        "vga",
    }
    ct_keys = {
        "arch",
        "cores",
        "cpuunits",
        "cpulimit",
        "memory",
        "swap",
        "hostname",
        "ostype",
        "unprivileged",
    }
    kept, removed = [], []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            raise Failure("Unexpected snapshot section in restored configuration")
        key, sep, value = line.partition(":")
        if not sep:
            raise Failure("Malformed restored configuration")
        value = value.strip()
        disk = (
            bool(re.fullmatch(r"(?:ide|sata|scsi|virtio)\d+|efidisk0|tpmstate0", key))
            if kind == "vm"
            else bool(re.fullmatch(r"rootfs|mp\d+", key))
        )
        if disk:
            if "media=cdrom" in value:
                removed.append(key)
                continue
            if not re.match(
                rf"{re.escape(storage)}:(?:{temporary_id}/)?(?:vm|subvol)-{temporary_id}-",
                value,
            ):
                raise Failure(
                    f"{key} is not an owned temporary volume on target storage"
                )
            kept.append(f"{key}: {value}")
        elif key in (vm_keys if kind == "vm" else ct_keys):
            kept.append(f"{key}: {value}")
        else:
            removed.append(key)
    kept.extend(["onboot: 0", "protection: 0"])
    return "\n".join(kept) + "\n", removed


def host_cpu_capacity() -> int:
    capacity = os.cpu_count()
    if hasattr(os, "sched_getaffinity"):
        capacity = min(capacity or 0, len(os.sched_getaffinity(0)))
    if not capacity:
        raise Failure("Cannot determine DR host CPU capacity")
    return capacity


def normalize_vm_cpus(
    config: str, host_vcpus: int, max_test_vcpus: int | None
) -> tuple[str, str | None]:
    """Cap the temporary VM's maximum topology and initial hotplug count."""
    lines = config.splitlines()
    fields = {}
    for index, line in enumerate(lines):
        key, _, value = line.partition(":")
        if key not in ("cores", "sockets", "vcpus"):
            continue
        if key in fields or not re.fullmatch(r"[1-9]\d*", value.strip()):
            raise Failure(f"Invalid restored VM {key}")
        fields[key] = (index, int(value.strip()))
    cores = fields.get("cores", (None, 1))[1]
    sockets = fields.get("sockets", (None, 1))[1]
    source_max = cores * sockets
    initial = fields.get("vcpus", (None, source_max))[1]
    if initial > source_max:
        raise Failure("Restored VM vcpus exceeds cores * sockets")
    limit = min(host_vcpus, max_test_vcpus or host_vcpus)
    if source_max <= limit:
        return config, None
    # A single socket gives an exact cap even when the old topology does not
    # divide the smaller host's thread count.
    replacements = {"cores": limit, "sockets": 1}
    if "vcpus" in fields:
        replacements["vcpus"] = min(initial, limit)
    for key, value in replacements.items():
        if key in fields:
            lines[fields[key][0]] = f"{key}: {value}"
        else:
            lines.append(f"{key}: {value}")
    reason = (
        "configured test limit"
        if max_test_vcpus is not None
        and max_test_vcpus < host_vcpus
        and max_test_vcpus < source_max
        else "DR host capacity"
    )
    adjustment = f"CPU adjusted: {source_max} -> {limit} vCPU"
    if "vcpus" in fields and initial != source_max:
        adjustment += f" maximum; initial vcpus: {initial} -> {min(initial, limit)}"
    return "\n".join(lines) + "\n", f"{adjustment}; reason: {reason}"


class Runner:
    def __init__(self, config: Config):
        self.config = config
        self.base = Path("/etc/pve")

    def paths(self) -> list[Path]:
        return [
            self.base / folder / f"{self.config.temporary_id}.conf"
            for folder in ("qemu-server", "lxc")
        ]

    def preflight(self) -> None:
        if os.geteuid() != 0 or socket.gethostname() != self.config.hostname:
            raise Failure("Requires root on the configured test host")
        if (self.base / "corosync.conf").exists():
            raise Failure("Clustered hosts are not permitted")
        if any(path.exists() for path in self.paths()):
            raise Failure("Temporary ID already exists; refusing to touch it")
        stores = json.loads(
            command(["pvesh", "get", "/storage", "--output-format", "json"])
        )
        if not any(
            s["storage"] == self.config.pbs_storage and s["type"] == "pbs"
            for s in stores
        ):
            raise Failure("Source storage must be PBS")
        if not any(
            s["storage"] == self.config.target_storage
            and s["type"] in ("dir", "lvmthin", "zfspool")
            for s in stores
        ):
            raise Failure("Target must be dedicated local dir/lvmthin/zfspool storage")

    def test(self, guest: Guest) -> tuple[str, str, bool]:
        cfg = self.config
        tool = "qm" if guest.kind == "vm" else "pct"
        vmid = str(cfg.temporary_id)
        path = self.paths()[0 if guest.kind == "vm" else 1]
        details = [f"{guest.kind}/{guest.source_id} {guest.name}"]
        status, safe = "OK", True
        owned = False
        try:
            if any(p.exists() for p in self.paths()):
                return "FAIL", details[0] + ": temporary ID occupied", False
            host_vcpus = host_cpu_capacity() if guest.kind == "vm" else 0
            volume = latest_backup(
                command(
                    [
                        "pvesh",
                        "get",
                        f"/nodes/{cfg.hostname}/storage/{cfg.pbs_storage}/content",
                        "--content",
                        "backup",
                        "--output-format",
                        "json",
                    ]
                ),
                cfg.pbs_storage,
                guest,
            )
            details.append(f"Backup: {volume}")
            if guest.kind == "ct":
                original = command(["pvesm", "extractconfig", volume])
                for line in original.splitlines():
                    key, _, value = line.partition(":")
                    if re.fullmatch(r"mp\d+", key) and value.strip().startswith("/"):
                        raise Failure(
                            "LXC host bind/device mount requires a backup without host mounts"
                        )
            start = time.monotonic()
            owned = True  # also clean partially restored guests on CLI failure
            args = (
                ["qmrestore", volume, vmid]
                if guest.kind == "vm"
                else ["pct", "restore", vmid, volume]
            )
            command([*args, "--storage", cfg.target_storage], cfg.restore_timeout)
            details.append(f"Restore: OK ({time.monotonic() - start:.1f}s)")
            clean, removed = sanitize(
                path.read_text(), guest.kind, cfg.target_storage, cfg.temporary_id
            )
            if guest.kind == "vm":
                clean, cpu_adjustment = normalize_vm_cpus(
                    clean, host_vcpus, cfg.max_test_vcpus
                )
                if cpu_adjustment:
                    details.append(cpu_adjustment)
            path.write_text(clean)
            if path.read_text() != clean:
                raise Failure("Sanitized configuration verification failed")
            if removed:
                details.append("Removed options: " + ", ".join(removed))
            start = time.monotonic()
            deadline = start + guest.boot_timeout
            command([tool, "start", vmid], max(1, deadline - time.monotonic()))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Failure("Boot/health deadline exceeded")
                try:
                    state = command([tool, "status", vmid], min(10, remaining))
                    if state.strip() != "status: running":
                        raise Failure("Guest is not running")
                    if guest.kind == "ct":
                        command(
                            ["pct", "exec", vmid, "--", "/bin/true"], min(10, remaining)
                        )
                    elif guest.agent:
                        command(["qm", "agent", vmid, "ping"], min(10, remaining))
                    break
                except Failure:
                    time.sleep(min(2, max(0, deadline - time.monotonic())))
            details.append(
                f"Boot: OK ({time.monotonic() - start:.1f}s); "
                + (
                    "LXC exec: OK"
                    if guest.kind == "ct"
                    else "QGA: OK"
                    if guest.agent
                    else "QGA: not requested (running state only)"
                )
            )
            if guest.kind == "vm" and not guest.agent:
                status = "WARN"
        except (Failure, OSError, ValueError, KeyError) as error:
            status = "FAIL"
            details.append(str(error))
        finally:
            if owned and path.exists():
                try:
                    # Never run backup hooks during stop/destroy, even after partial restore.
                    lines = path.read_text().splitlines()
                    # Refuse destruction if partial restore retained foreign disk references.
                    for line in lines:
                        key, _, value = line.partition(":")
                        if (
                            re.fullmatch(
                                r"(?:ide|sata|scsi|virtio|unused|mp)\d+|rootfs|efidisk0|tpmstate0",
                                key,
                            )
                            and "media=cdrom" not in value
                            and not re.match(
                                rf"{re.escape(cfg.target_storage)}:(?:{vmid}/)?(?:vm|subvol)-{vmid}-",
                                value.strip(),
                            )
                        ):
                            raise Failure(
                                "Foreign disk reference; manual cleanup required"
                            )
                    path.write_text(
                        "\n".join(
                            line
                            for line in lines
                            if not line.startswith(("hookscript:", "protection:"))
                        )
                        + "\n"
                    )
                    state = command([tool, "status", vmid])
                    if state.strip() != "status: stopped":
                        command([tool, "stop", vmid], 120)
                    command([tool, "destroy", vmid], 120)
                    if any(p.exists() for p in self.paths()):
                        raise Failure("Temporary configuration still exists")
                    details.append("Cleanup: OK")
                except (Failure, OSError) as error:
                    status, safe = "FAIL", False
                    details.append(f"Cleanup: {error}; remaining guests skipped")
            elif owned:
                status, safe = "FAIL", False
                details.append(
                    "No restored configuration; inspect orphan volumes before retrying"
                )
        return status, "\n".join(details), safe


def run(config: Config, no_poweroff: bool) -> int:
    runner = Runner(config)
    runner.preflight()  # Never power off/report on wrong host or ID collision.
    results: list[tuple[str, str]] = []
    safe = True
    for guest in config.guests:
        if not safe:
            results.append(("SKIP", f"{guest.kind}/{guest.source_id}: unsafe cleanup"))
            continue
        status, details, safe = runner.test(guest)
        results.append((status, details))
    totals = Counter(status for status, _ in results)
    summary = ", ".join(
        f"{key}={totals[key]}" for key in ("OK", "WARN", "FAIL", "SKIP")
    )
    report = (
        summary
        + "\n\n"
        + "\n\n".join(f"{status}\n{details}" for status, details in results)
    )
    print(report, flush=True)
    result = int(bool(totals["FAIL"] or totals["SKIP"]))
    notifier = Path(__file__).resolve().parent.parent / "mail-notifier" / "send-mail.sh"
    try:
        command(
            [
                str(notifier),
                "--to",
                config.mail_to,
                "--subject",
                f"Proxmox restore check: {summary}",
            ],
            60,
            report,
        )
    except (Failure, OSError):
        print("Summary mail failed; report remains in journal", flush=True)
        result = 1
    if config.poweroff and not no_poweroff:
        try:
            command(["systemctl", "poweroff"])
        except (Failure, OSError):
            print("Poweroff failed", flush=True)
            result = 1
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--no-poweroff", action="store_true")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        with Path("/run/proxmox-restore-check.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return run(config, args.no_poweroff)
    except (Failure, OSError, ValueError, TypeError, KeyError) as error:
        print(f"Restore check refused/failed: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
