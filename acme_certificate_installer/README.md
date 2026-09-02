# ACME Certificate Installer

Helper script for renewing ECC certificates with `acme.sh`, rebuilding HAProxy PEM files and reloading HAProxy when a certificate is renewed.

## Requirements

- Python 3
- `acme.sh` installed under the executing user's home directory
- HAProxy
- systemd
- Permission to write certificates under `/etc/haproxy/certs/acme`
- Permission to reload HAProxy

Install the Python dependency:

```bash
python3 -m pip install -r requirements.txt
```

## Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

Configure the domains as a JSON array:

```ini
DOMAINS=["example.com","service.example.com"]
```

The script loads `.env` from its own directory.

## Usage

Run the script from an account whose home directory contains the required `acme.sh` installation and which has the permissions needed to write HAProxy certificates and reload the service:

```bash
python3 acme_certificate_installer.py
```

For each configured domain, the script:

1. runs `acme.sh --renew` using ECC certificates;
2. skips domains that are not due for renewal;
3. installs the full chain and key into the corresponding `acme.sh` certificate directory;
4. combines the full chain and private key into `/etc/haproxy/certs/acme/<domain>.pem`;
5. reloads HAProxy once if at least one certificate was renewed successfully.

## Logs

The script writes output to the terminal and to:

```text
acme_install.log
```

The log file is recreated on each execution.

## Notes

The script expects `acme.sh` at:

```text
$HOME/.acme.sh/acme.sh
```

and ECC certificate data under:

```text
$HOME/.acme.sh/<domain>_ecc
```

Runtime credentials, certificates and local `.env` files must not be committed to the repository.
