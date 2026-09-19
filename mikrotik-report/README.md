# MikroTik blocking report

A small Python 3 collector polls RouterOS over its HTTPS REST API and saves IPv4
drop-rule counters in a local SQLite database. A separate command emails one
summary for each completed Monday-to-Monday week through the repository's
`mail-notifier/send-mail.sh`. Install it on any Linux host that can reach the
router; no server names or credentials are built into the program.

The two report sections have different meanings. The local rule measures packets
discarded by the router's own detection list. Bouncer rules measure traffic
discarded under CrowdSec decisions; CrowdSec detects and classifies those
attacks. Counts are packets and bytes, not unique IPs or attacks. The collector
never enables per-packet logging.

## Requirements and configuration

* Python 3.9 or newer, with timezone data for `MIKROTIK_REPORT_TIMEZONE`.
* RouterOS 7 with `www-ssl` enabled, a certificate trusted by the collecting
  host, and a dedicated account permitted to read firewall rules, address lists,
  and system resource data. On the tested RouterOS 7.24.4 installation, a custom
  group with `read,api,rest-api` worked; `read,rest-api` alone returned
  `not allowed (9)`. Restrict the account to the collecting host with its
  `address` setting (`10.1.1.11/32` in that installation; use the actual
  collector address elsewhere).
* `mail-notifier/send-mail.sh` in the same checkout, plus its configured `msmtp`
  account on the collecting host. Grant the weekly service's dedicated
  `mail-notifier` group access to the SMTP configuration as described in the
  [mail notifier setup](../mail-notifier/README.md#allow-a-non-root-service-to-send).

Copy `.env.example` to a private `.env` and adapt every required value. The
example selects the local `raw` rule by exact comment and source address list.
Change `MIKROTIK_LOCAL_RULE_TABLE` to `filter` if the local drop rule is there.
The bouncer selector matches the configured signature within a rule comment,
the configured source address list, and `action=drop` in both IPv4 `raw` and
`filter` tables. This includes only source blocking, not output-chain rules
using a destination list. An absent bouncer rule contributes zero until it
returns. The local rule must match exactly once; missing or duplicate local
rules make collection fail so a bad selector cannot silently report zero.

`MIKROTIK_REST_URL` must be an HTTPS URL ending in `/rest`. TLS verification
remains enabled. For a private CA, set `MIKROTIK_CA_FILE` to its PEM bundle or
install it in the system trust store. The collector makes five small read-only
requests per run: IPv4 `raw` rules, IPv4 `filter` rules, each selected address
list, and router uptime. Rule and list responses request only needed fields.
The list requests return one minimal record per entry to determine their size.

Use an absolute `MIKROTIK_REPORT_DB` path outside the checkout. The SQLite
database contains only last counter values and current/pending weekly totals.
The service user needs write access to its parent directory. The program sets
the database file to mode `600`. Keep `.env` private as it contains the RouterOS
password; neither it nor the database belongs in Git.

## Installation

The templates are examples; replace `/path/to/self-hosted` and `YOUR_USER` in
both services. Choose a user that can read `.env` and write the state directory.
The collector's `StateDirectory=mikrotik-report` creates
`/var/lib/mikrotik-report` for `MIKROTIK_REPORT_DB`. Configure the SMTP group
before enabling the weekly timer. The weekly service runs as the same
unprivileged user with `mail-notifier` added only to its process. It has no
`StateDirectory` so systemd does not change ownership of the collector's
directory. Set the timezone in `.env` to the intended reporting timezone, for
example `Europe/Madrid`.
If the weekly timer runs before the first collection, it exits without creating
the database; the collector creates it under its own user.

```bash
cd /path/to/self-hosted/mikrotik-report
cp .env.example .env
chmod 600 .env
# Edit .env and both service templates for this host.
sudo install -m 644 mikrotik-report-collect.service.example /etc/systemd/system/mikrotik-report-collect.service
sudo install -m 644 mikrotik-report-collect.timer.example /etc/systemd/system/mikrotik-report-collect.timer
sudo install -m 644 mikrotik-report-weekly.service.example /etc/systemd/system/mikrotik-report-weekly.service
sudo install -m 644 mikrotik-report-weekly.timer.example /etc/systemd/system/mikrotik-report-weekly.timer
sudo systemctl daemon-reload
sudo systemctl enable --now mikrotik-report-collect.timer mikrotik-report-weekly.timer
```

For an existing installation where the weekly unit runs as `root`, first grant
the SMTP group access described above. Then update the installed weekly unit to
match the template while preserving its local user and paths, and run
`sudo systemctl daemon-reload`. The collector unit and database ownership stay
with the collector user.

The collector timer first runs two minutes after boot, then five minutes after
each activation. The report timer checks at 00:15 every day in the host's local
timezone, with `Persistent=true` to catch missed runs after downtime. It only
sends completed weeks. Daily checks retry pending mail after a transport
failure; normally one email is sent per week. Align the host timezone and
`MIKROTIK_REPORT_TIMEZONE` if the first check should occur soon after the week
closes.

For a manual run, use the same environment and user as the service:

```bash
sudo systemctl start mikrotik-report-collect.service
sudo systemctl start mikrotik-report-weekly.service
sudo journalctl -u mikrotik-report-collect.service -u mikrotik-report-weekly.service -n 100
```

To test the complete path before the week closes, run a transient service with
the same user, group, and environment as the weekly service. Do this after at
least one successful collection:

```bash
sudo systemd-run --wait --collect --pipe \
  -p User=YOUR_USER \
  -p Group=YOUR_USER \
  -p SupplementaryGroups=mail-notifier \
  -p WorkingDirectory=/path/to/self-hosted/mikrotik-report \
  -p EnvironmentFile=/path/to/self-hosted/mikrotik-report/.env \
  /usr/bin/python3 /path/to/self-hosted/mikrotik-report/mikrotik_report.py test-report
```

`test-report` fetches a live RouterOS sample, reads the SQLite database in
read-only mode, calculates a preview of the current incomplete week in memory,
and sends it through `mail-notifier` to `MIKROTIK_REPORT_TO` with a `[TEST]`
subject and a clear test banner. It does not update counters, close a week, or
remove pending reports. Success prints `Sent test report`; a RouterOS or mail
failure exits nonzero. The preview may include the latest observed counter
delta, which the next scheduled collection will still record normally.

The first collection establishes a baseline; it does not claim traffic that
occurred before installation. Each later sample adds the difference from the
previous value. A lower counter or a detected router reboot starts a new
counter sequence. A newly observed rule starts with a fresh baseline to avoid
counting traffic that may have occurred before the collector saw it. If a rule
disappears, its last baseline is discarded. The report includes observed
router reboots, other counter resets, and new or recreated rule baselines.

Weekly boundaries use `MIKROTIK_REPORT_TIMEZONE`; a counter difference is
credited to the week in which its later sample occurs. Polling gaps, rules
created and removed between samples, and a reset followed by a counter that
already exceeds the old value without a detectable reboot can undercount or
hide a reset. They cannot be reconstructed from periodic counters. The report
shows the number of collector samples so missing coverage is visible. A week
with no samples is not evidence of zero blocked traffic.

SQLite transactions serialize collection and reporting, and state survives
host restarts. A completed week stays pending if email delivery fails; the
report command exits nonzero and the next timer run retries. If the process
crashes after the mailer accepts a message but before SQLite records success,
the next run can send that week again. No additional SMTP mechanism is used.

To recover, fix the router connection or mail configuration and restart the
failed service. Back up the SQLite file with SQLite's backup API or while both
timers are stopped; do not copy it during a write. To reset all reporting
history, stop both timers, move the database aside, and start them again. The
next collection will establish a new baseline.

## Local validation

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q mikrotik_report.py
```

The tests use synthetic RouterOS responses and temporary SQLite files. They
do not contact the router or send email. This repository change does not enable
RouterOS `www-ssl`, create an account, or install systemd units on a live host.

RouterOS REST behavior and rule counters are documented by [MikroTik REST API](https://manual.mikrotik.com/docs/developer-guides/rest-api/)
and [MikroTik firewall matchers](https://manual.mikrotik.com/docs/firewall-and-quality-of-service/firewall/common-firewall-matchers-and-actions/).
The user group policies and address restriction are documented by [MikroTik User](https://manual.mikrotik.com/docs/authentication-authorization-accounting/user/).
