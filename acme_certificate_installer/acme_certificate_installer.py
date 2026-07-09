import subprocess
import sys
from pathlib import Path
from dotenv import load_dotenv
import os
import json
import logging


LOG_FILE = Path(__file__).parent / "acme_install.log"
LOG_FILE.write_text("", encoding="utf-8")
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger()

load_dotenv()
domains_str = os.getenv("DOMAINS", "[]")
try:
    domains = json.loads(domains_str)
except Exception:
    raise ValueError("DOMAINS is not in the correct format. It should be a JSON array string.")

print(f"domains: {domains}")

#home = os.environ["HOME"]
home = os.environ["HOME"]
print(f"home: {home}")
acmeSh = f"{home}/.acme.sh/acme.sh"
print(f"acmeSh: {acmeSh}")

haproxy_needs_reload = False

for domain in domains:
    acmeHome = f"{home}/.acme.sh/{domain}_ecc"
    cert_dest = f"/etc/haproxy/certs/acme/{domain}.pem"
    fullchain = f"{acmeHome}/fullchain.cer"
    keyfile = f"{acmeHome}/{domain}.key"

    # 1) Renew (ECC)
    renew = subprocess.run(
        [acmeSh, "--renew", "-d", domain, "--ecc"],
        capture_output=True,
        text=True
    )

    if renew.returncode not in (0, 2):
        log.error(f"Renew failed for {domain} (exit code {renew.returncode})")
        if renew.stdout:
            log.error(f"Renew stdout for {domain}:\n{renew.stdout}")
        if renew.stderr:
            log.error(f"Renew stderr for {domain}:\n{renew.stderr}")
        continue

    if renew.returncode == 2:
        log.info(f"Renew skipped for {domain} (not due)")
        continue
    
    log.info(f"Renewed {domain}:\n{renew.stdout}")

    
    # 2) Install cert files into the expected paths inside acme home
    try:
        install = subprocess.run(
            [
                acmeSh,
                "--install-cert",
                "-d",
                domain,
                "--fullchain-file",
                fullchain,
                "--key-file",
                keyfile,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        log.info(f"Install output for {domain}:\n{install.stdout}")
        if install.stderr:
            log.error(f"Install errors for {domain}:\n{install.stderr}")
    except subprocess.CalledProcessError as e:
        log.error(f"Install failed for {domain} (exit code {e.returncode}):\n{e.stderr}")
        continue

    # 3) Build HAProxy PEM (fullchain + key)
    try:
        with open(fullchain, "rb") as f1, open(keyfile, "rb") as f2:
            pem_bytes = f1.read() + f2.read()
        
        subprocess.run(
            ["sudo", "tee", cert_dest],
            input=pem_bytes,
            check=True,
            stdout=subprocess.DEVNULL,
        )    
        log.info(f"HAProxy PEM written for {domain} to {cert_dest}")
        haproxy_needs_reload = True
    except subprocess.CalledProcessError as e:
        log.error(f"Error creating HAProxy PEM for {domain}:\n{e.stderr}")
        continue
    
if haproxy_needs_reload:
    try:
        reload_result = subprocess.run(
            ["sudo", "systemctl", "reload", "haproxy"],
            check=True,
            capture_output=True,
            text=True,
        )
        log.info("HAProxy reloaded successfully.")
        if reload_result.stdout:
            log.info(reload_result.stdout)
        if reload_result.stderr:
            log.error(reload_result.stderr)
    except subprocess.CalledProcessError as e:
        log.error(f"HAProxy reload failed (exit code {e.returncode}):\n{e.stderr}")
