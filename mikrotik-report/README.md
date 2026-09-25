# MikroTik report deployment

This directory contains only the Docker Compose deployment for
[`raposo93/mikrotik-report`](https://github.com/Raposo93/mikrotik-report). The
application source, tests, image build, and release lifecycle belong to that
standalone repository.

The deployment pins the published `0.1.3` image. Image upgrades should be made
explicitly in both `docker-compose.yml` and `.env.example` so they remain
reviewable.

## Configure and start

Copy the example configuration, restrict it, and replace every required value:

```bash
cp .env.example .env
chmod 600 .env
docker compose pull
docker compose up -d
```

## Optional private RouterOS CA

The Compose definition always exposes the container path
`/etc/ssl/certs/mikrotik-ca.crt`, but defaults its host source to `/dev/null`.
This keeps the deployment optional: with `MIKROTIK_CA_FILE` unset, the
application uses the container's system trust store and ignores the placeholder
mount.

When RouterOS uses a private CA, set both values in the private `.env`:

```text
MIKROTIK_CA_HOST_PATH=/usr/local/share/ca-certificates/crowdsec-routeros-ca.crt
MIKROTIK_CA_FILE=/etc/ssl/certs/mikrotik-ca.crt
```

The host path must be absolute and point to an existing regular file. Compose
has `create_host_path: false`, so a typo fails instead of silently creating a
directory. Recreate the container after changing the mount, then verify it and
run one collection:

```bash
docker compose config
docker compose up -d --force-recreate
docker compose run --rm --entrypoint sh mikrotik-report -c \
  'ls -l /etc/ssl/certs/mikrotik-ca.crt'
docker compose run --rm mikrotik-report collect
```

The image includes its own SMTP notifier. Configure `MIKROTIK_SMTP_HOST`
and, when authentication is required, `MIKROTIK_SMTP_USER` plus either
`MIKROTIK_SMTP_PASSWORD` or a mounted `MIKROTIK_SMTP_PASSWORD_FILE`. STARTTLS on
port 587 is the default; use `MIKROTIK_SMTP_TLS=implicit` for direct TLS on port
465. TLS certificate verification cannot be disabled.

ASN enrichment is disabled by default. Enabling `MIKROTIK_ASN_ENABLED` permits
outbound Team Cymru bulk WHOIS lookups in addition to RouterOS and SMTP access.

The `report-data` named volume stores the SQLite database. Do not run the old
systemd timers and this container against the same database. When migrating an
existing `/var/lib/mikrotik-report/report.sqlite3`, stop the old timers first
and copy the database into the Compose volume while preserving ownership for
UID/GID `10001`.

## Upgrade to 0.1.3

Version `0.1.3` atomically migrates the SQLite schema from version 5 to 6 on its
first writing command. Stop every collector and report process and copy the
database out of the named volume before pulling the image:

```bash
docker compose stop mikrotik-report
docker compose cp \
  mikrotik-report:/var/lib/mikrotik-report/report.sqlite3 \
  ./report.sqlite3.v0.1.2
docker compose pull
docker compose up -d
```

Keep that backup until collection and reporting have succeeded. Version `0.1.2`
rejects the migrated schema; rollback requires stopping `0.1.3` and restoring
the pre-upgrade database before starting the old image. Never downgrade
SQLite's `user_version` manually. Existing independent source and destination
totals remain available, but correlated per-source destination history begins
only with events collected after the migration.

View service status and logs with:

```bash
docker compose ps
docker compose logs --tail=100 mikrotik-report
```

The container publishes no inbound ports. It needs outbound HTTPS access to the
RouterOS REST API and whatever external resources its notifier uses.
