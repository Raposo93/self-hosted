#!/usr/bin/python3
"""Report pending security/vendor updates without installing packages."""

import argparse
import importlib
import os
import platform
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


def security_origin(origin: Any) -> bool:
    """Use signed Release metadata, not repository URL substring matches."""
    return bool(
        origin.trusted
        and (
            (origin.origin == "Debian" and origin.archive.endswith("-security"))
            or (
                origin.origin in {"Ubuntu", "UbuntuESM", "UbuntuESMApps"}
                and origin.archive.endswith("-security")
            )
        )
    )


def pending_updates(
    packages: Iterable[Any],
    compare: Callable[[str, str], int],
    proxmox: bool,
    held_names: frozenset[str] = frozenset(),
) -> list[str]:
    updates = []
    for package in packages:
        if not package.is_installed:
            continue
        installed = package.installed.version
        relevant = []
        for version in package.versions:
            if compare(version.version, installed) <= 0:
                continue
            if any(security_origin(origin) for origin in version.origins):
                relevant.append((version.version, "security"))
            elif (
                proxmox
                and any(origin.trusted for origin in version.origins)
                and (
                    package.name.startswith(
                        ("pve-", "proxmox-", "libpve-", "libproxmox-")
                    )
                    or any(
                        origin.trusted and origin.origin == "Proxmox"
                        for origin in version.origins
                    )
                )
            ):
                relevant.append((version.version, "Proxmox review"))
        if relevant:
            # A newer non-security candidate may already contain this fix.
            version, reason = relevant[0]
            candidate = package.candidate.version if package.candidate else "none"
            held = (
                "; held"
                if package.name in held_names or package.fullname in held_names
                else ""
            )
            updates.append(
                f"{package.fullname}: {installed} -> {version} ({reason}; "
                f"APT candidate: {candidate}{held})"
            )
    return sorted(updates)


def reboot_reasons(
    compare: Callable[[str, str], int],
    running: str,
    boot_time: int,
    boot_directory: Path = Path("/boot"),
    run_directory: Path = Path("/run"),
) -> list[str]:
    reasons = []
    if (run_directory / "reboot-required").exists():
        reasons.append("/run/reboot-required is present")
        packages = run_directory / "reboot-required.pkgs"
        if packages.exists():
            reasons.extend(
                f"Reboot requested by: {name}"
                for name in packages.read_text().splitlines()
            )
    # Debian/Proxmox need not create Ubuntu's reboot-required marker.
    for image in sorted(boot_directory.glob("vmlinuz-*")):
        release = image.name.removeprefix("vmlinuz-")
        if compare(release, running) > 0:
            reasons.append(
                f"Installed kernel {release} is newer than running {running}"
            )
        elif release == running and image.stat().st_mtime > boot_time:
            reasons.append(f"Running kernel image {release} changed since boot")
    return reasons


def report(hostname: str, updates: list[str], reboot: list[str]) -> tuple[str, str]:
    states = []
    if updates:
        states.append("SECURITY UPDATES")
    if reboot:
        states.append("REBOOT REQUIRED")
    state = " / ".join(states) or "OK"
    body = "\n".join(
        [f"Host: {hostname}", f"State: {state}", "", *updates, "", *reboot]
    )
    return state, body


def send_notification(state: str, body: str, hostname: str) -> None:
    recipient = os.environ.get("RECIPIENT_EMAIL", "").strip()
    if not recipient:
        raise ValueError("RECIPIENT_EMAIL is required for notifications")
    notifier = Path(__file__).resolve().parent.parent / "mail-notifier/send-mail.sh"
    command = [str(notifier), "--to", recipient, "--subject", f"[{hostname}] {state}"]
    for variable, flag in (("MSMTP_ACCOUNT", "--account"), ("SENDER_EMAIL", "--from")):
        if os.environ.get(variable):
            command.extend([flag, os.environ[variable]])
    subprocess.run(command, input=body, text=True, check=True, timeout=120)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-refresh", action="store_true", help="use existing APT lists"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print report without sending mail"
    )
    args = parser.parse_args()
    try:
        if not args.dry_run and not os.environ.get("RECIPIENT_EMAIL", "").strip():
            raise ValueError("RECIPIENT_EMAIL is required for notifications")
        apt = importlib.import_module("apt")
        apt_pkg = importlib.import_module("apt_pkg")
        if not args.no_refresh:
            subprocess.run(
                ["apt-get", "-o", "APT::Update::Error-Mode=any", "update"],
                stdout=subprocess.DEVNULL,
                check=True,
                timeout=900,
            )
        cache = apt.Cache()
        proxmox = any(
            name in cache and cache[name].is_installed
            for name in ("proxmox-ve", "pve-manager", "proxmox-backup-server")
        )
        held_names = frozenset(
            package.name
            for package in apt_pkg.Cache(None).packages
            if package.selected_state == apt_pkg.SELSTATE_HOLD
        )
        updates = pending_updates(cache, apt_pkg.version_compare, proxmox, held_names)
        boot_time = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
        reboot = reboot_reasons(apt_pkg.version_compare, platform.release(), boot_time)
        hostname = platform.node()
        state, body = report(hostname, updates, reboot)
        if args.dry_run:
            print(body)
        elif updates or reboot:
            print(body, flush=True)
            send_notification(state, body, hostname)
        return 0
    except (
        ImportError,
        OSError,
        ValueError,
        SystemError,
        StopIteration,
        subprocess.SubprocessError,
    ) as error:
        # Avoid dumping subprocess arguments (mail headers may contain private data).
        print(
            f"Security update check failed ({type(error).__name__}); check dependencies, APT and notifier configuration.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
