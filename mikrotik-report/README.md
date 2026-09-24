# MikroTik report deployment

This directory contains only the Docker Compose deployment for
[`raposo93/mikrotik-report`](https://github.com/Raposo93/mikrotik-report). The
application source, tests, image build, and release lifecycle belong to that
standalone repository.

The deployment pins the published `0.1.0` image. Image upgrades should be made
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

`MIKROTIK_REPORT_NOTIFIER_HOST_PATH` must be an absolute host path to an
executable compatible with the notifier interface documented upstream. The
container mounts it read-only as `/usr/local/bin/send-mail`. The executable and
everything it needs must work inside the container and be readable and
executable by UID/GID `10001`. The image does not contain an SMTP client; the
repository's host `mail-notifier/send-mail.sh` therefore cannot be mounted
directly unless the image is extended with its `bash` and `msmtp` runtime
requirements.

The `report-data` named volume stores the SQLite database. Do not run the old
systemd timers and this container against the same database. When migrating an
existing `/var/lib/mikrotik-report/report.sqlite3`, stop the old timers first
and copy the database into the Compose volume while preserving ownership for
UID/GID `10001`. Keep the original database until the container has collected
and reported successfully.

View service status and logs with:

```bash
docker compose ps
docker compose logs --tail=100 mikrotik-report
```

The container publishes no inbound ports. It needs outbound HTTPS access to the
RouterOS REST API and whatever external resources its notifier uses.
