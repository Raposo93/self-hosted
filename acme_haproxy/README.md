# ACME HAProxy

Helper script for issuing and renewing ECC certificates with `acme.sh`,
installing them for HAProxy, rebuilding PEM files, and reloading HAProxy
when certificates are deployed.

## Requirements

* Python 3
* `acme.sh`
* HAProxy
* systemd
* HTTP-01 webroot at `/var/www/acme-challenges`
* Permission to read `/etc/haproxy/maps/hosts.map`
* Permission to write ACME challenge files under `/var/www/acme-challenges`
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

The first field is used as the domain name for certificate issuance and renewal.

The HAProxy host map is the source of truth for domains managed by this tool.

A domain must exist in the map before a certificate can be issued for it.

## Commands

### Issue a certificate

Issue and deploy a certificate for a domain already present in the HAProxy host map:

```bash
sudo HOME=/path/to/acme-user-home \
  python3 acme_haproxy.py issue example.com
```

The `issue` command:

1. checks that the domain exists in `/etc/haproxy/maps/hosts.map`;
2. runs `acme.sh --issue` using HTTP-01 with `/var/www/acme-challenges`;
3. installs the full chain and private key;
4. builds `/etc/haproxy/certs/acme/<domain>.pem`;
5. sets the PEM permissions to `0600`;
6. reloads HAProxy.

The usual workflow for publishing a new domain is:

1. configure the HAProxy backend;
2. add the domain to `/etc/haproxy/maps/hosts.map`;
3. run `acme_haproxy.py issue <domain>`;
4. verify HTTPS.

### Renew certificates

Renew all managed certificates:

```bash
sudo HOME=/path/to/acme-user-home \
  python3 acme_haproxy.py renew
```

The `renew` command processes every domain found in the HAProxy host map.

Domains whose certificates are not due for renewal are skipped.

HAProxy is reloaded once after all successfully renewed certificates have been deployed.

## systemd

The `renew` command can be automated through the provided systemd service and timer.

Certificate issuance with `issue` remains an explicit operation when publishing a new domain.

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

## How it works

Both `issue` and `renew` use:

```text
/etc/haproxy/maps/hosts.map
```

as the source of truth for managed domains.

### Issue flow

For the requested domain, the script:

1. verifies that the domain exists in the HAProxy host map;
2. runs `acme.sh --issue` using ECC certificates and HTTP-01;
3. installs the full chain and private key;
4. combines them into `/etc/haproxy/certs/acme/<domain>.pem`;
5. sets the PEM permissions to `0600`;
6. reloads HAProxy.

### Renew flow

For each domain found in the map, the script:

1. runs `acme.sh --renew` using ECC certificates;
2. skips domains that are not due for renewal;
3. installs the full chain and private key when a certificate is renewed;
4. combines them into `/etc/haproxy/certs/acme/<domain>.pem`;
5. sets the PEM permissions to `0600`;
6. reloads HAProxy once if at least one certificate was renewed successfully.

If any operation fails, the script exits with a non-zero status.

When executed through systemd, this causes the service to be reported as failed.

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

The HTTP-01 webroot is:

```text
/var/www/acme-challenges
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
