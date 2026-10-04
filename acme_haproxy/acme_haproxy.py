#!/usr/bin/env python3

import argparse
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger()

HAPROXY_CONFIG = Path("/etc/haproxy/haproxy.cfg")
HAPROXY_HOSTS_MAP = Path("/etc/haproxy/maps/hosts.map")
ACME_WEBROOT = Path("/var/www/acme-challenges")
PENDING_STATE = Path("/etc/haproxy/certs/acme/.acme-haproxy-pending.json")


class LockFailure(RuntimeError):
    pass


@contextmanager
def operation_lock() -> Iterator[None]:
    # Keep a stable inode: locking the atomically replaced state file is unsafe.
    path = PENDING_STATE.with_name(".acme-haproxy.lock")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        raise LockFailure(
            f"Cannot open ACME operation lock at {path}: {error}"
        ) from error
    with os.fdopen(fd, "r+") as stream:
        log.info("Waiting for ACME operation lock at %s", path)
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            raise LockFailure(
                f"Cannot acquire ACME operation lock at {path}: {error}"
            ) from error
        log.info("Acquired ACME operation lock")
        yield


class PendingState:
    def __init__(self) -> None:
        self.phases: dict[str, str] = (
            json.loads(PENDING_STATE.read_text()) if PENDING_STATE.exists() else {}
        )
        if not isinstance(self.phases, dict) or any(
            phase not in {"renew", "deploy", "validate", "reload"}
            for phase in self.phases.values()
        ):
            raise ValueError(f"Invalid pending state in {PENDING_STATE}")

    def set(self, domain: str, phase: str) -> None:
        self.phases[domain] = phase
        self._save()
        log.info("Pending %s for %s", phase, domain)

    def clear(self, domain: str) -> None:
        del self.phases[domain]
        self._save()

    def _save(self) -> None:
        if not self.phases:
            PENDING_STATE.unlink(missing_ok=True)
            return
        fd, name = tempfile.mkstemp(prefix=".acme-state-", dir=PENDING_STATE.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.phases, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, PENDING_STATE)
        finally:
            Path(name).unlink(missing_ok=True)


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

    issue_parser = subparsers.add_parser(
        "issue",
        help="Issue a new ACME certificate",
        description="Issue and deploy a certificate for an HAProxy domain",
    )
    issue_parser.add_argument(
        "domain",
        help="Domain to issue the certificate for",
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
    backup = _backup_path(cert_dest)
    if cert_dest.exists() and not backup.exists():
        _atomic_copy(cert_dest, backup)
    elif not cert_dest.exists():
        _new_cert_marker(cert_dest).touch(mode=0o600, exist_ok=True)

    fd, name = tempfile.mkstemp(prefix=".acme-pem-", dir=cert_dest.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(pem_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, cert_dest)
    finally:
        Path(name).unlink(missing_ok=True)

    log.info("HAProxy PEM replaced at %s", cert_dest)


def _backup_path(cert_dest: Path) -> Path:
    return cert_dest.with_name(f".{cert_dest.name}.previous")


def _new_cert_marker(cert_dest: Path) -> Path:
    return cert_dest.with_name(f".{cert_dest.name}.new")


def _atomic_copy(source: Path, destination: Path) -> None:
    fd, name = tempfile.mkstemp(prefix=".acme-copy-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream, source.open("rb") as original:
            shutil.copyfileobj(original, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def _restore_pem(cert_dest: Path) -> None:
    backup = _backup_path(cert_dest)
    if backup.exists():
        os.replace(backup, cert_dest)
        log.warning("Restored previous HAProxy PEM at %s", cert_dest)
    elif _new_cert_marker(cert_dest).exists():
        cert_dest.unlink(missing_ok=True)
        _new_cert_marker(cert_dest).unlink()
        log.warning("Removed unvalidated HAProxy PEM at %s", cert_dest)
    else:
        raise FileNotFoundError(
            f"No previous PEM or new certificate marker for {cert_dest}"
        )


def _validate_haproxy_config() -> None:
    result = subprocess.run(
        [
            "haproxy",
            "-c",
            "-f",
            str(HAPROXY_CONFIG),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    log.info("HAProxy configuration validated successfully.")

    if result.stdout:
        log.info(result.stdout)

    if result.stderr:
        log.info(result.stderr)


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


def _deploy_certificate(
    acme_sh: Path,
    home: Path,
    domain: str,
) -> None:
    paths = _get_certificate_paths(home, domain)
    log.info("Deploying certificate for %s", domain)
    try:
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
    except (OSError, subprocess.CalledProcessError) as e:
        log.error("Certificate deployment failed for %s: %s", domain, e)
        raise
    log.info("Certificate deployed for %s", domain)


def _source_differs_from_pem(home: Path, domain: str) -> bool:
    paths = _get_certificate_paths(home, domain)
    if not paths["fullchain"].exists() or not paths["keyfile"].exists():
        return False
    expected = paths["fullchain"].read_bytes() + paths["keyfile"].read_bytes()
    return (
        not paths["cert_dest"].exists() or paths["cert_dest"].read_bytes() != expected
    )


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


def _issue_certificate(
    acme_sh: Path,
    domain: str,
) -> None:
    result = subprocess.run(
        [
            str(acme_sh),
            "--issue",
            "-d",
            domain,
            "-w",
            str(ACME_WEBROOT),
            "--ecc",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    log.info(f"Issued certificate for {domain}")

    if result.stdout:
        log.info(result.stdout)

    if result.stderr:
        log.info(result.stderr)


def renew_all() -> int:
    try:
        with operation_lock():
            return _renew_all_locked()
    except LockFailure as error:
        log.error("%s", error)
        return 1


def _renew_all_locked() -> int:
    domains = _load_domains(HAPROXY_HOSTS_MAP)

    home = Path(os.environ["HOME"])
    acme_sh = home / ".acme.sh" / "acme.sh"

    log.info(f"Domains: {domains}")
    log.info(f"Home: {home}")
    log.info(f"acme.sh: {acme_sh}")

    state = PendingState()
    had_errors = False

    for domain in domains:
        try:
            phase = state.phases.get(domain)
            if phase is None:
                state.set(domain, "renew")
                phase = "renew"
            if phase == "renew":
                renewed = _renew_certificate(acme_sh, domain)
                if not renewed and not _source_differs_from_pem(home, domain):
                    state.clear(domain)
                    continue
                state.set(domain, "deploy")
            elif phase != "deploy":
                log.info("Resuming pending %s for %s", phase, domain)
                continue
            else:
                log.info("Retrying pending deployment for %s", domain)

            _deploy_certificate(acme_sh, home, domain)
            state.set(domain, "validate")

        except (RuntimeError, subprocess.CalledProcessError, OSError) as e:
            log.error(f"Failed to process {domain}: {e}")
            had_errors = True

    if _finish_pending(state, home):
        had_errors = True

    return 1 if had_errors else 0


def _finish_pending(state: PendingState, home: Path) -> bool:
    ready = {
        domain: phase
        for domain, phase in state.phases.items()
        if phase in {"validate", "reload"}
    }
    if not ready:
        return bool(state.phases)

    try:
        _validate_haproxy_config()
    except (OSError, subprocess.CalledProcessError) as e:
        log.error("HAProxy validation failed: %s", e)
        for domain in ready:
            try:
                state.set(domain, "deploy")
                _restore_pem(_get_certificate_paths(home, domain)["cert_dest"])
            except OSError as restore_error:
                log.error(
                    "Failed to restore previous PEM for %s: %s", domain, restore_error
                )
        return True

    try:
        for domain, phase in ready.items():
            if phase == "validate":
                state.set(domain, "reload")
        _reload_haproxy()
    except (OSError, subprocess.CalledProcessError) as e:
        log.error("HAProxy reload failed: %s", e)
        return True

    for domain in ready:
        try:
            _backup_path(_get_certificate_paths(home, domain)["cert_dest"]).unlink(
                missing_ok=True
            )
            _new_cert_marker(_get_certificate_paths(home, domain)["cert_dest"]).unlink(
                missing_ok=True
            )
            state.clear(domain)
        except OSError as e:
            log.error("Failed to clear pending reload for %s: %s", domain, e)
            return True
    return bool(state.phases)


def issue(domain: str) -> int:
    try:
        with operation_lock():
            return _issue_locked(domain)
    except LockFailure as error:
        log.error("%s", error)
        return 1


def _issue_locked(domain: str) -> int:
    domains = _load_domains(HAPROXY_HOSTS_MAP)

    if domain not in domains:
        log.error(f"Domain {domain} is not configured in {HAPROXY_HOSTS_MAP}")
        return 1

    home = Path(os.environ["HOME"])
    acme_sh = home / ".acme.sh" / "acme.sh"

    log.info(f"Domain: {domain}")
    log.info(f"Home: {home}")
    log.info(f"acme.sh: {acme_sh}")
    log.info(f"ACME webroot: {ACME_WEBROOT}")

    try:
        state = PendingState()
        _issue_certificate(acme_sh, domain)
        state.set(domain, "deploy")
        _deploy_certificate(acme_sh, home, domain)
        state.set(domain, "validate")
        return 1 if _finish_pending(state, home) else 0

    except subprocess.CalledProcessError as e:
        log.error(
            f"Failed to issue or deploy {domain} "
            f"(exit code {e.returncode}):\n{e.stderr}"
        )
        return 1

    except OSError as e:
        log.error(f"Failed to issue or deploy {domain}: {e}")
        return 1

    return 0


def main() -> int:
    args = _parse_args()

    if args.command == "renew":
        return renew_all()

    if args.command == "issue":
        return issue(args.domain)

    return 1


if __name__ == "__main__":
    sys.exit(main())
