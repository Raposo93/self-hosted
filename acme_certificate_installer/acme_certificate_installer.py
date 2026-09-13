import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv


logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger()

env_file = Path(__file__).with_name(".env")
load_dotenv(env_file)

domains_str = os.getenv("DOMAINS", "[]")

try:
    domains = json.loads(domains_str)
except json.JSONDecodeError as e:
    raise ValueError(
        "DOMAINS is not in the correct format. It should be a JSON array string."
    ) from e

home = Path(os.environ["HOME"])
acme_sh = home / ".acme.sh" / "acme.sh"

log.info(f"Domains: {domains}")
log.info(f"Home: {home}")
log.info(f"acme.sh: {acme_sh}")

had_errors = False
haproxy_needs_reload = False

for domain in domains:
    acme_home = home / ".acme.sh" / f"{domain}_ecc"
    cert_dest = Path("/etc/haproxy/certs/acme") / f"{domain}.pem"
    fullchain = acme_home / "fullchain.cer"
    keyfile = acme_home / f"{domain}.key"

    # 1) Renew certificate
    renew = subprocess.run(
        [str(acme_sh), "--renew", "-d", domain, "--ecc"],
        capture_output=True,
        text=True,
    )

    if renew.returncode not in (0, 2):
        log.error(
            f"Renew failed for {domain} "
            f"(exit code {renew.returncode})"
        )

        if renew.stdout:
            log.error(f"Renew stdout for {domain}:\n{renew.stdout}")

        if renew.stderr:
            log.error(f"Renew stderr for {domain}:\n{renew.stderr}")

        had_errors = True
        continue

    if renew.returncode == 2:
        log.info(f"Renew skipped for {domain} (not due)")
        continue

    log.info(f"Renewed {domain}")

    if renew.stdout:
        log.info(renew.stdout)

    # 2) Install certificate files
    try:
        install = subprocess.run(
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

        if install.stdout:
            log.info(f"Install output for {domain}:\n{install.stdout}")

        if install.stderr:
            log.info(f"Install stderr for {domain}:\n{install.stderr}")

    except subprocess.CalledProcessError as e:
        log.error(
            f"Install failed for {domain} "
            f"(exit code {e.returncode}):\n{e.stderr}"
        )
        had_errors = True
        continue

    # 3) Build HAProxy PEM
    try:
        pem_bytes = fullchain.read_bytes() + keyfile.read_bytes()
        cert_dest.write_bytes(pem_bytes)
        cert_dest.chmod(0o600)

        log.info(
            f"HAProxy PEM written for {domain} to {cert_dest}"
        )

        haproxy_needs_reload = True

    except OSError as e:
        log.error(f"Error creating HAProxy PEM for {domain}: {e}")
        had_errors = True
        continue

if haproxy_needs_reload:
    try:
        reload_result = subprocess.run(
            ["systemctl", "reload", "haproxy"],
            check=True,
            capture_output=True,
            text=True,
        )

        log.info("HAProxy reloaded successfully.")

        if reload_result.stdout:
            log.info(reload_result.stdout)

        if reload_result.stderr:
            log.info(reload_result.stderr)

    except subprocess.CalledProcessError as e:
        log.error(
            f"HAProxy reload failed "
            f"(exit code {e.returncode}):\n{e.stderr}"
        )
        had_errors = True

if had_errors:
    sys.exit(1)
