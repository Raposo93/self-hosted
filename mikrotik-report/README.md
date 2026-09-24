# MikroTik report deployment

This directory contains only the Docker Compose deployment for
[`raposo93/mikrotik-report`](https://github.com/Raposo93/mikrotik-report). The
application source, tests, image build, and release lifecycle belong to that
standalone repository.

The deployment pins the published `0.1.2` image. Image upgrades should be made
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

Release `0.1.2` includes its own SMTP notifier. Configure `MIKROTIK_SMTP_HOST`
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
UID/GID `10001`. Keep the original database until the container has collected
and reported successfully. Upgrading from `0.1.0` to `0.1.2` does not require a
database migration.

View service status and logs with:

```bash
docker compose ps
docker compose logs --tail=100 mikrotik-report
```

The container publishes no inbound ports. It needs outbound HTTPS access to the
RouterOS REST API and whatever external resources its notifier uses.
