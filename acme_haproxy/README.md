# ACME HAProxy

Helper script for issuing and renewing ECC certificates with `acme.sh`,
installing them for HAProxy, rebuilding PEM files, and reloading HAProxy
when certificates are deployed.

For a fresh Debian/Ubuntu installation with no certificates, follow the
[HAProxy bootstrap guide](../haproxy/README.md) first. It provides the
HTTP-only configuration, challenge server, `acme.sh` installation, first
certificate, HTTPS transition, and rollback. This page describes the helper
and the renewal units used by that guide.

## Requirements

* Python 3
* `acme.sh`
* HAProxy
* systemd
* Running HTTP-01 challenge server and webroot at `/var/www/acme-challenges`
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

`issue` and `renew` share an exclusive file lock at
`/etc/haproxy/certs/acme/.acme-haproxy.lock`. A second invocation, including a
manual command while the service runs, waits up to 30 seconds for the first to
finish, then exits non-zero with a retry message if still busy. After acquiring
the lock it
reads the latest pending state before doing any certificate work. The lock file
remains on disk; the operating system releases the lock when the process exits,
including after an error. Do not delete the lock file while an operation runs.
If the lock cannot be opened or acquired, the command reports an error and exits
non-zero.

## Time limits

External commands have finite limits: issuance/renewal 600 seconds per domain,
installation 60 seconds per domain, validation 30 seconds, and reload 60 seconds.
Override them with positive integer options **before** the subcommand:

```bash
sudo HOME=/path/to/acme-user-home python3 acme_haproxy.py \
  --acme-timeout 900 --install-timeout 90 --validate-timeout 45 \
  --reload-timeout 90 --lock-timeout 10 renew
```

Timeouts fail the operation and preserve pending work. A validation timeout
restores previous PEMs just like a validation error; a reload timeout leaves
`reload` pending and retains the previous PEM backup. A timed-out issuance can
be retried with `issue`; `renew` also reconciles certificates written before an
interrupted command finished. No successful validation or reload is recorded
until its command exits successfully.

On command timeout, SIGTERM, or Ctrl+C, the helper kills the external command's
process group (including ordinary descendants) and waits for its direct child
before releasing the lock. Commands must not detach into another session.
The service also uses `KillMode=control-group` to stop all processes in its
cgroup, with `TimeoutStopSec=10s` as the final cleanup bound. Killing the
`systemctl` client cannot undo a reload job already submitted to systemd;
inspect HAProxy status after a reload timeout and retry pending work.

The service template sets `TimeoutStartSec=1h` as a global bound. Adjust it in
the installed unit for the number of domains: allow at least
`lock timeout + N × (ACME timeout + install timeout) + validation timeout + reload timeout`,
plus time for filesystem work. With defaults, five domains need 3420 seconds
plus that margin; larger maps need a longer global limit. Add overridden
command options to `ExecStart` and reload systemd after editing the unit.
A global stop may interrupt any phase; inspect the journal and run `renew`
after correcting the cause to resume the recorded pending work.

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
4. builds `/etc/haproxy/certs/acme/<domain>.pem` with permissions `0600`;
5. validates the HAProxy configuration;
6. reloads HAProxy.

On the first issue, the active HAProxy configuration must be the HTTP-only
bootstrap example. Its challenge backend remains available while the helper
validates and reloads. After issue succeeds, install the HTTPS configuration
as described in the [bootstrap guide](../haproxy/README.md#5-activate-https).

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

Domains whose certificates are not due for renewal and have no pending work are skipped.

HAProxy is reloaded once after all successfully renewed or pending certificates have
been deployed and the configuration has been validated.

## systemd

The `renew` command can be automated through the provided systemd service and timer.

Certificate issuance with `issue` remains an explicit operation when publishing a new domain.

### Install the service

From the repository root, copy the service template:

```bash
sudo cp acme_haproxy/acme-haproxy.service.example \
  /etc/systemd/system/acme-haproxy.service
```

Edit the installed service and replace:

```text
/path/to/self-hosted/acme_haproxy
/path/to/acme-user-home
```

with the actual paths. Use `/var/lib/acme-haproxy` for `HOME` when following
the bootstrap guide. The unit runs as root because it writes private keys and
PEMs and reloads HAProxy.

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
sudo cp acme_haproxy/acme-haproxy.timer.example \
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
systemctl is-enabled acme-haproxy.timer
systemctl is-active acme-haproxy.timer
```

The example timer runs once per day and uses `Persistent=true`, so a missed execution is triggered after the system becomes available again.
The `acme.sh` installer must use `--no-cron`, as in the bootstrap guide.
On an existing installation, check `sudo crontab -l` and other ACME timers
for duplicate renewal schedules before enabling this timer. Keep a single
scheduler for these certificates; `acme-haproxy.timer` owns renewal here.

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
4. combines them into a temporary PEM with permissions `0600`, then atomically
   replaces `/etc/haproxy/certs/acme/<domain>.pem`;
5. validates the HAProxy configuration, restoring the previous PEM if validation
   fails;
6. reloads HAProxy.

### Renew flow

For each domain found in the map, the script:

1. runs `acme.sh --renew` using ECC certificates;
2. skips domains that are not due for renewal;
3. installs the full chain and private key when a certificate is renewed;
4. combines them into a temporary PEM with permissions `0600`, then atomically
   replaces `/etc/haproxy/certs/acme/<domain>.pem`;
5. validates the HAProxy configuration, restoring previous PEMs if validation
   fails;
6. reloads HAProxy once if any certificate was deployed or a reload is pending.

If any operation fails, the script exits with a non-zero status.

When executed through systemd, this causes the service to be reported as failed.

## Pending work and recovery

Pending work is recorded in `/etc/haproxy/certs/acme/.acme-haproxy-pending.json`.
Each domain has a phase: `renew`, `deploy`, `validate`, or `reload`. The file is
removed after all work succeeds. Hidden `.pem.previous` files hold previous PEMs
until validation and reload succeed; hidden `.pem.new` markers identify initial
issuance where there was no previous PEM. These files contain or refer to private
keys and must remain outside Git and readable only by the service operator.

If deployment or reload fails, inspect the journal and the pending file, fix the
reported cause, then run the `renew` command again. It retries pending work even
when `acme.sh` says a renewal is not due. A failed `issue` command may also be
retried with `issue <domain>` if issuance itself did not succeed. A repeated
failure keeps the service result non-zero and leaves the pending phase visible.
Do not delete the pending file or previous PEM while investigating a failure.

The script validates before each reload. On validation failure it restores the
previous PEM, or removes a new unvalidated PEM. If restoration itself fails, the
journal reports the error and the pending phase remains for investigation. A
failed reload leaves the validated PEM in place and retries validation and
reload on the next run. No reload occurs when nothing changed and no work is
pending.

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
