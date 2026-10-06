"""Run one Proxmox Backup Client profile and report its overall result."""

import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

MAILER = Path(__file__).resolve().parent.parent / "mail-notifier/send-mail.sh"


class ConfigurationError(ValueError):
    """Invalid profile or unavailable input, safe to display in diagnostics."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class Config:
    source_argument: str
    repository: str
    archive: str
    backup_id: str | None
    namespace: str
    included_mounts: list[Path]
    keyfile: str
    pre_hook: str
    post_hook: str
    healthcheck_url: str


@dataclass
class Result:
    pre_exit: int | None = None
    backup_exit: int | None = None
    post_exit: int | None = None
    pre_configured: bool = False
    post_configured: bool = False
    prepared: bool = False
    backup_attempted: bool = False
    cancellation: int = 0
    cancellation_name: str = ""
    configuration_error: str = ""
    configuration_exit: int = 1

    def exit_code(self) -> int:
        if self.cancellation:
            return self.cancellation
        if self.configuration_error:
            return self.configuration_exit
        for code in (self.pre_exit, self.backup_exit, self.post_exit):
            if code:
                return code
        return 0

    def subject(self, archive: str, host: str) -> str:
        if self.cancellation:
            description = "[ERROR] Backup cancelled"
        elif self.configuration_error:
            description = "[ERROR] Backup not started; configuration error"
        elif self.pre_exit:
            description = "[ERROR] Backup not started; pre-hook failed"
        elif self.backup_exit:
            description = "[ERROR] Backup failed"
        elif self.post_exit:
            description = "[WARN] Backup completed; post-hook failed"
        else:
            description = "[OK] Backup completed"
        return f"{description}: {archive} on {host}"


class Cancellation:
    def __init__(self, result: Result) -> None:
        self.result = result
        self.client: subprocess.Popen[bytes] | None = None

    def handle(self, signum: int, _frame: object) -> None:
        if not self.result.cancellation:
            self.result.cancellation = 128 + signum
            self.result.cancellation_name = signal.Signals(signum).name
        self.terminate_client()

    def terminate_client(self) -> None:
        if self.client is not None and self.client.poll() is None:
            try:
                self.client.terminate()
            except ProcessLookupError:
                pass


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ConfigurationError(f"Missing {name}")
    return value


def find_mount(path: Path, selector: str, field: str) -> str:
    try:
        process = subprocess.run(
            ["findmnt", selector, str(path), "--noheadings", "--output", field],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise ConfigurationError("Cannot run findmnt") from error
    if process.returncode:
        raise ConfigurationError(
            f"Cannot determine the mount {field.lower()} for {path}"
        )
    return process.stdout.strip()


def existing_directory(value: str, name: str) -> Path:
    path = Path(value)
    if not path.is_dir():
        raise ConfigurationError(f"{name} must name an existing directory: {value}")
    return path.resolve(strict=True)


def validate_hook(value: str, name: str) -> None:
    if value and (not Path(value).is_file() or not os.access(value, os.X_OK)):
        raise ConfigurationError(f"{name} is not an executable file: {value}", 126)


def load_config() -> Config:
    source_argument = required("SOURCE_DIR")
    source = existing_directory(source_argument, "Source directory")
    repository = required("REPO")
    archive = required("BACKUP_NAME")
    required("RECIPIENT_EMAIL")
    required("SENDER_EMAIL")
    required("MSMTP_ACCOUNT")

    backup_id = os.environ.get("BACKUP_ID")
    if backup_id is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", backup_id
    ):
        raise ConfigurationError(
            "BACKUP_ID must start with an alphanumeric character and contain "
            "only letters, digits, _, . or -"
        )

    if "EXPECTED_MOUNT" in os.environ:
        mount_value = os.environ["EXPECTED_MOUNT"]
        if not mount_value or not Path(mount_value).is_dir():
            raise ConfigurationError("EXPECTED_MOUNT must name an existing directory")
        mount = Path(mount_value).resolve(strict=True)
        if not source.is_relative_to(mount):
            raise ConfigurationError(
                f"SOURCE_DIR is outside EXPECTED_MOUNT: {mount_value}"
            )
        actual_mount = find_mount(source, "--target", "TARGET")
        if actual_mount != str(mount):
            raise ConfigurationError(
                f"Expected mount {mount} is absent from SOURCE_DIR "
                f"(found {actual_mount})"
            )
        if "EXPECTED_MOUNT_SOURCE" in os.environ:
            expected_source = os.environ["EXPECTED_MOUNT_SOURCE"]
            if not expected_source:
                raise ConfigurationError("EXPECTED_MOUNT_SOURCE must not be empty")
            actual_source = find_mount(source, "--target", "SOURCE")
            if actual_source != expected_source:
                raise ConfigurationError(
                    f"Unexpected mount source for {mount}: {actual_source}"
                )
    elif "EXPECTED_MOUNT_SOURCE" in os.environ:
        raise ConfigurationError("EXPECTED_MOUNT_SOURCE requires EXPECTED_MOUNT")

    included_mounts: list[Path] = []
    mounts_value = os.environ.get("INCLUDE_DEV_MOUNTS", "")
    if mounts_value:
        if (
            mounts_value.startswith("|")
            or mounts_value.endswith("|")
            or "||" in mounts_value
        ):
            raise ConfigurationError("INCLUDE_DEV_MOUNTS contains an empty entry")
        for entry in mounts_value.split("|"):
            if not Path(entry).is_absolute() or not Path(entry).is_dir():
                raise ConfigurationError(
                    f"INCLUDE_DEV_MOUNTS entries must be existing absolute directories: {entry}"
                )
            included = Path(entry).resolve(strict=True)
            if included == source or not included.is_relative_to(source):
                raise ConfigurationError(
                    f"INCLUDE_DEV_MOUNTS entry is outside SOURCE_DIR: {entry}"
                )
            try:
                actual_mount = find_mount(included, "--mountpoint", "TARGET")
            except ConfigurationError as error:
                raise ConfigurationError(
                    f"INCLUDE_DEV_MOUNTS entry is not mounted: {entry}"
                ) from error
            if actual_mount != str(included):
                raise ConfigurationError(
                    f"INCLUDE_DEV_MOUNTS entry is not mounted: {entry}"
                )
            included_mounts.append(included)

    keyfile = os.environ.get("ENCRYPTION_KEYFILE", "")
    credential_directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    credential_present = (
        bool(credential_directory)
        and (
            Path(credential_directory) / "proxmox-backup-client.encryption-password"
        ).is_file()
    )
    if bool(keyfile) != credential_present:
        raise ConfigurationError("Incomplete encryption configuration")
    if keyfile and (not Path(keyfile).is_file() or not os.access(keyfile, os.R_OK)):
        raise ConfigurationError(
            f"Encryption key file not found or not readable: {keyfile}"
        )

    pre_hook = os.environ.get("PRE_BACKUP_HOOK", "")
    post_hook = os.environ.get("POST_BACKUP_HOOK", "")
    validate_hook(pre_hook, "Pre-backup hook")
    validate_hook(post_hook, "Post-backup hook")
    return Config(
        source_argument,
        repository,
        archive,
        backup_id,
        os.environ.get("NAMESPACE", ""),
        included_mounts,
        keyfile,
        pre_hook,
        post_hook,
        os.environ.get("HEALTHCHECK_URL", ""),
    )


def log(stream: TextIO, message: str) -> None:
    print(message, file=stream, flush=True)


def command_exit(command: list[str], stream: TextIO) -> int:
    try:
        process = subprocess.run(
            command, stdout=stream, stderr=subprocess.STDOUT, check=False
        )
    except FileNotFoundError:
        log(stream, f"Command not found: {command[0]}")
        return 127
    except OSError as error:
        log(stream, f"Cannot run {command[0]}: {error.strerror}")
        return 126
    return process.returncode if process.returncode >= 0 else 128 - process.returncode


def run_hook(path: str, phase: str, stream: TextIO) -> int:
    log(stream, f"Running {phase}-backup hook: {path}")
    code = command_exit([path], stream)
    log(stream, f"{phase.capitalize()}-backup hook exit code: {code}")
    return code


def client_command(config: Config) -> list[str]:
    command = [
        "proxmox-backup-client",
        "backup",
        f"{config.archive}:{config.source_argument}",
        "--repository",
        config.repository,
    ]
    if config.backup_id is not None:
        command.extend(["--backup-id", config.backup_id])
    if config.namespace:
        command.extend(["--ns", config.namespace])
    if config.keyfile:
        command.extend(["--keyfile", config.keyfile])
    for mount in config.included_mounts:
        command.extend(["--include-dev", str(mount)])
    command.extend(["--change-detection-mode", "metadata", "--skip-e2big-xattr"])
    return command


def run_client(
    config: Config, result: Result, cancellation: Cancellation, stream: TextIO
) -> None:
    result.backup_attempted = True
    environment = os.environ.copy()
    # NAMESPACE is the only namespace setting, including an empty root.
    environment.pop("PBS_NAMESPACE", None)
    try:
        cancellation.client = subprocess.Popen(
            client_command(config),
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError:
        log(stream, "Command not found: proxmox-backup-client")
        result.backup_exit = 127
        return
    except OSError as error:
        log(stream, f"Cannot run proxmox-backup-client: {error.strerror}")
        result.backup_exit = 126
        return
    try:
        if result.cancellation:
            cancellation.terminate_client()
        code = cancellation.client.wait()
        result.backup_exit = code if code >= 0 else 128 - code
    finally:
        cancellation.client = None


def report_health(url: str, phase: str, stream: TextIO) -> None:
    endpoint = url.rstrip("/")
    if phase != "success":
        endpoint += f"/{phase}"
    try:
        response = subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--output",
                "/dev/null",
                "--connect-timeout",
                "5",
                "--max-time",
                "10",
                endpoint,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=12,
            check=False,
        )
        if response.returncode:
            log(
                stream,
                f"Warning: Healthcheck {phase} failed (curl exit code: {response.returncode})",
            )
        else:
            log(stream, f"Healthcheck {phase} reported")
    except (OSError, subprocess.TimeoutExpired):
        log(
            stream,
            f"Warning: Healthcheck {phase} failed (curl unavailable or timed out)",
        )


def report_phases(result: Result, stream: TextIO, duration: int) -> None:
    if result.configuration_error:
        log(stream, f"Configuration error: {result.configuration_error}")
    if result.cancellation:
        log(stream, f"Backup cancelled by {result.cancellation_name}")
    if result.pre_exit is None:
        log(
            stream,
            "Pre-backup hook: not run"
            if result.pre_configured
            else "Pre-backup hook: not configured",
        )
    else:
        log(stream, f"Pre-backup hook exit code: {result.pre_exit}")
    if result.backup_attempted:
        log(stream, f"Backup exit code: {result.backup_exit}")
    else:
        log(stream, "Backup not started")
    if result.post_exit is None:
        log(
            stream,
            "Post-backup hook: not run"
            if result.post_configured
            else "Post-backup hook: not configured",
        )
    else:
        log(stream, f"Post-backup hook exit code: {result.post_exit}")
    log(stream, f"Overall exit code: {result.exit_code()}")
    log(stream, f"Duration: {duration}s")
    log(
        stream,
        f"========== Backup ended at {datetime.now().astimezone():%Y-%m-%d %H:%M:%S} ==========",
    )


def send_mail(log_path: Path, subject: str, stream: TextIO) -> None:
    required_names = ("MSMTP_ACCOUNT", "SENDER_EMAIL", "RECIPIENT_EMAIL")
    if any(not os.environ.get(name) for name in required_names):
        log(
            stream,
            "Warning: Cannot send notification email; mail configuration is incomplete",
        )
        return
    stream.flush()
    try:
        with log_path.open("rb") as body:
            response = subprocess.run(
                [
                    str(MAILER),
                    "--account",
                    os.environ["MSMTP_ACCOUNT"],
                    "--from",
                    os.environ["SENDER_EMAIL"],
                    "--to",
                    os.environ["RECIPIENT_EMAIL"],
                    "--subject",
                    subject,
                ],
                stdin=body,
                stdout=subprocess.DEVNULL,
                stderr=stream,
                check=False,
            )
        if response.returncode:
            log(
                stream,
                f"Warning: Failed to send notification email (exit code: {response.returncode})",
            )
    except OSError as error:
        log(stream, f"Warning: Failed to send notification email: {error.strerror}")


def main() -> int:
    os.umask(0o077)
    try:
        log_path = Path(required("LOGFILE"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = log_path.open("w", encoding="utf-8", errors="replace")
    except (ConfigurationError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    result = Result(
        pre_configured=bool(os.environ.get("PRE_BACKUP_HOOK")),
        post_configured=bool(os.environ.get("POST_BACKUP_HOOK")),
    )
    cancellation = Cancellation(result)
    signal.signal(signal.SIGTERM, cancellation.handle)
    signal.signal(signal.SIGINT, cancellation.handle)
    started = time.monotonic()
    host = socket.getfqdn() or socket.gethostname()
    archive = os.environ.get("BACKUP_NAME", "unknown archive")
    with stream:
        log(
            stream,
            f"========== Backup started at {datetime.now().astimezone():%Y-%m-%d %H:%M:%S} ==========",
        )
        log(stream, f"Host: {host}")
        log(stream, f"Source: {os.environ.get('SOURCE_DIR', '(missing)')}")
        log(stream, f"Archive: {archive}")
        if "BACKUP_ID" in os.environ:
            log(stream, f"Backup ID: {os.environ['BACKUP_ID']}")
        config: Config | None = None
        try:
            config = load_config()
            if config.pre_hook and not result.cancellation:
                result.pre_exit = run_hook(config.pre_hook, "pre", stream)
            if not config.pre_hook or result.pre_exit == 0:
                result.prepared = True
                if config.healthcheck_url and not result.cancellation:
                    report_health(config.healthcheck_url, "start", stream)
                if not result.cancellation:
                    run_client(config, result, cancellation, stream)
        except ConfigurationError as error:
            result.configuration_error = str(error)
            result.configuration_exit = error.exit_code
            print(f"Error: {error}", file=sys.stderr)
        finally:
            if config is not None and result.prepared and config.post_hook:
                result.post_exit = run_hook(config.post_hook, "post", stream)

        report_phases(result, stream, int(time.monotonic() - started))
        healthcheck_url = os.environ.get("HEALTHCHECK_URL", "")
        if healthcheck_url:
            report_health(
                healthcheck_url,
                "success" if result.exit_code() == 0 else "fail",
                stream,
            )
        send_mail(log_path, result.subject(archive, host), stream)
    return result.exit_code()


if __name__ == "__main__":
    sys.exit(main())
