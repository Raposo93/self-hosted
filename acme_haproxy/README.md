# ACME HAProxy

Helper script for renewing ECC certificates with `acme.sh`,
installing them for HAProxy, rebuilding PEM files, and reloading
HAProxy when a certificate is renewed.

## Requirements

* Python 3
* `acme.sh`
* HAProxy
* systemd
* Permission to read `/etc/haproxy/maps/hosts.map`
* Permission to write certificates under `/etc/haproxy/certs/acme`
* Permission to reload HAProxy

## Configuration

Managed domains are read from the HAProxy host map:

```text
/etc/haproxy/maps/hosts.map
```

Each non-empty, non-comment line must contain a domain and its backend:

```text
example.com example_backend
service.example.com service_backend
```

Blank lines and lines beginning with `#` are ignored.

The first field is used as the domain name for certificate renewal.

The HAProxy host map is the source of truth for domains managed by this tool.

## systemd

The recommended way to run the tool is through the provided systemd service and timer.

### Install the service

Copy the service template:

```bash
sudo cp acme-haproxy.service.example \
  /etc/systemd/system/acme-haproxy.service
```

Edit the installed service and replace:

```text
/path/to/self-hosted/acme_haproxy
/path/to/acme-user-home
```

with the actual paths.

The `HOME` value must point to the home directory containing the `acme.sh` installation.

For example:

```text
$HOME/.acme.sh/acme.sh
```

Reload systemd:

```bash
sudo systemctl daemon-reload
```

### Test the service

Run the service manually before enabling the timer:

```bash
sudo systemctl start acme-haproxy.service
sudo systemctl status acme-haproxy.service
```

View its logs with:

```bash
journalctl -u acme-haproxy.service
```

### Install the timer

Copy the timer template:

```bash
sudo cp acme-haproxy.timer.example \
  /etc/systemd/system/acme-haproxy.timer
```

Reload systemd:

```bash
sudo systemctl daemon-reload
```

Enable and start the timer:

```bash
sudo systemctl enable --now acme-haproxy.timer
```

Check the next scheduled execution:

```bash
systemctl list-timers acme-haproxy.timer
```

The example timer runs once per day and uses `Persistent=true`, so a missed execution is triggered after the system becomes available again.

## Manual usage

The script can also be executed manually for testing.

Run it with a `HOME` that contains the expected `acme.sh` installation and with sufficient privileges to read the HAProxy host map, write HAProxy certificates, and reload the service:

```bash
sudo HOME=/path/to/acme-user-home \
  python3 acme_haproxy.py renew
```

## How it works

The `renew` command reads the managed domains from:

```text
/etc/haproxy/maps/hosts.map
```

For each domain found in the map, the script:

1. runs `acme.sh --renew` using ECC certificates;
2. skips domains that are not due for renewal;
3. installs the full chain and private key into the corresponding `acme.sh` certificate directory;
4. combines the full chain and private key into `/etc/haproxy/certs/acme/<domain>.pem`;
5. reloads HAProxy once if at least one certificate was renewed successfully.

If any operation fails, the script exits with a non-zero status so systemd reports the service as failed.

## Logs

The script writes its logs to standard output.

When executed through systemd, logs are available through the journal:

```bash
journalctl -u acme-haproxy.service
```

Show the latest entries:

```bash
journalctl -u acme-haproxy.service -n 100
```

Show entries from the current day:

```bash
journalctl -u acme-haproxy.service --since today
```

## Paths

HAProxy host mappings are read from:

```text
/etc/haproxy/maps/hosts.map
```

The script expects `acme.sh` at:

```text
$HOME/.acme.sh/acme.sh
```

ECC certificate data is stored under:

```text
$HOME/.acme.sh/<domain>_ecc
```

HAProxy PEM files are written to:

```text
/etc/haproxy/certs/acme/<domain>.pem
```

## Security

Runtime credentials, certificates, and private keys must not be committed to the repository.
