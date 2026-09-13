#!/usr/bin/env python3

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger()

HAPROXY_HOSTS_MAP = Path("/etc/haproxy/maps/hosts.map")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage ACME certificates for HAProxy")

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    subparsers.add_parser(
        "renew",
        help="Renew configured ACME certificates",
        description="Renew all ACME certificates configured for HAProxy",
    )

    return parser.parse_args()


def _get_certificate_paths(home: Path, domain: str) -> dict[str, Path]:
    acme_home = home / ".acme.sh" / f"{domain}_ecc"

    return {
        "fullchain": acme_home / "fullchain.cer",
        "keyfile": acme_home / f"{domain}.key",
        "cert_dest": Path("/etc/haproxy/certs/acme") / f"{domain}.pem",
    }


def _renew_certificate(acme_sh: Path, domain: str) -> bool:
    result = subprocess.run(
        [str(acme_sh), "--renew", "-d", domain, "--ecc"],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode == 2:
        log.info(f"Renew skipped for {domain} (not due)")
        return False

    if result.returncode != 0:
        log.error(f"Renew failed for {domain} (exit code {result.returncode})")

        if result.stdout:
            log.error(f"Renew stdout for {domain}:\n{result.stdout}")

        if result.stderr:
            log.error(f"Renew stderr for {domain}:\n{result.stderr}")

        raise RuntimeError(f"Failed to renew {domain}")

    log.info(f"Renewed {domain}")

    if result.stdout:
        log.info(result.stdout)

    return True


def _install_certificate(
    acme_sh: Path,
    domain: str,
    fullchain: Path,
    keyfile: Path,
) -> None:
    result = subprocess.run(
        [
            str(acme_sh),
            "--install-cert",
            "-d",
            domain,
            "--ecc",
            "--fullchain-file",
            str(fullchain),
            "--key-file",
            str(keyfile),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    if result.stdout:
        log.info(f"Install output for {domain}:\n{result.stdout}")

    if result.stderr:
        log.info(f"Install stderr for {domain}:\n{result.stderr}")


def _build_haproxy_pem(
    fullchain: Path,
    keyfile: Path,
    cert_dest: Path,
) -> None:
    pem_bytes = fullchain.read_bytes() + keyfile.read_bytes()

    cert_dest.write_bytes(pem_bytes)
    cert_dest.chmod(0o600)

    log.info(f"HAProxy PEM written to {cert_dest}")


def _reload_haproxy() -> None:
    result = subprocess.run(
        ["systemctl", "reload", "haproxy"],
        check=True,
        capture_output=True,
        text=True,
    )

    log.info("HAProxy reloaded successfully.")

    if result.stdout:
        log.info(result.stdout)

    if result.stderr:
        log.info(result.stderr)


def _renew_domain(acme_sh: Path, home: Path, domain: str) -> bool:
    renewed = _renew_certificate(acme_sh, domain)

    if not renewed:
        return False

    paths = _get_certificate_paths(home, domain)

    _install_certificate(
        acme_sh,
        domain,
        paths["fullchain"],
        paths["keyfile"],
    )

    _build_haproxy_pem(
        paths["fullchain"],
        paths["keyfile"],
        paths["cert_dest"],
    )

    return True


def _load_domains(hosts_map: Path) -> list[str]:
    domains = []

    for line_number, raw_line in enumerate(
        hosts_map.read_text().splitlines(),
        start=1,
    ):
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        parts = line.split()

        if len(parts) != 2:
            raise ValueError(
                f"Invalid HAProxy map entry at {hosts_map}:{line_number}: {line!r}"
            )

        domain, _backend = parts
        domains.append(domain)

    return domains


def renew_all() -> int:
    domains = _load_domains(HAPROXY_HOSTS_MAP)

    home = Path(os.environ["HOME"])
    acme_sh = home / ".acme.sh" / "acme.sh"

    log.info(f"Domains: {domains}")
    log.info(f"Home: {home}")
    log.info(f"acme.sh: {acme_sh}")

    had_errors = False
    haproxy_needs_reload = False

    for domain in domains:
        try:
            if _renew_domain(acme_sh, home, domain):
                haproxy_needs_reload = True

        except (RuntimeError, subprocess.CalledProcessError, OSError) as e:
            log.error(f"Failed to process {domain}: {e}")
            had_errors = True

    if haproxy_needs_reload:
        try:
            _reload_haproxy()
        except subprocess.CalledProcessError as e:
            log.error(f"HAProxy reload failed (exit code {e.returncode}):\n{e.stderr}")
            had_errors = True

    return 1 if had_errors else 0


def main() -> int:
    args = _parse_args()

    if args.command == "renew":
        return renew_all()

    return 1


if __name__ == "__main__":
    sys.exit(main())
