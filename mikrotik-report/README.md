# MikroTik blocking report

A small Python 3 collector polls RouterOS over its HTTPS REST API and saves IPv4
drop-rule counters in a local SQLite database. Separate commands email summaries
for completed Monday-to-Monday weeks and calendar months through the repository's
`mail-notifier/send-mail.sh`; an interactive command renders explicit historical
date ranges to stdout. Install it on any Linux host that can reach the router;
no server names or credentials are built into the program.

The two report sections have different meanings. The local rule measures packets
discarded by the router's own detection list. Bouncer rules measure traffic
discarded under CrowdSec decisions; CrowdSec detects and classifies those
attacks. Counts are packets and bytes, not unique IPs or attacks. The collector
never enables per-packet logging.

## Requirements and configuration

* Python 3.10 or newer, with timezone data for `MIKROTIK_REPORT_TIMEZONE`.
* RouterOS 7 with `www-ssl` enabled, a certificate trusted by the collecting
  host, and a dedicated account permitted to read firewall rules, address lists,
  and system resource data. On the tested RouterOS 7.24.4 installation, a custom
  group with `read,api,rest-api` worked; `read,rest-api` alone returned
  `not allowed (9)`. Restrict the account to the collecting host with its
  `address` setting (`10.1.1.11/32` in that installation; use the actual
  collector address elsewhere).
* `mail-notifier/send-mail.sh` in the same checkout, plus its configured `msmtp`
  account on the collecting host. Grant both report services access to the SMTP
  password through the dedicated `mail-notifier` group as described in the
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
database contains last counter values, current/pending weekly totals, the last
12 successfully emailed weekly aggregates, and daily aggregates for exact month
boundaries and later historical queries. Monthly delivery status is stored in
the same database. The schema uses SQLite's `user_version`; a writing command
upgrades an older unversioned database before changing report state, while a
database created by a newer unsupported version is rejected. Daily aggregates
are retained without a time limit; they are small and contain no raw RouterOS
responses or individual samples. Existing weeks recorded before this feature
cannot be reconstructed into daily history.
The service user needs write access to its parent directory. The program sets
the database file to mode `600`. Keep `.env` private as it contains the RouterOS
password; neither it nor the database belongs in Git.

## Installation

The templates are examples; replace `/path/to/self-hosted` and `YOUR_USER` in
all three services. Choose a user that can read `.env` and write the state
directory.
The collector's `StateDirectory=mikrotik-report` creates
`/var/lib/mikrotik-report` for `MIKROTIK_REPORT_DB`. Configure SMTP password
access before enabling the report timers. The weekly and monthly services run
as the same unprivileged user with `mail-notifier` added only to their processes.
They have no `StateDirectory` so systemd does not change ownership of the
collector's directory. Set the timezone in `.env` to the intended reporting
timezone, for example `Europe/Madrid`.
If a report timer runs before the first collection, it exits without creating
the database; the collector creates it under its own user.

```bash
cd /path/to/self-hosted/mikrotik-report
cp .env.example .env
chmod 600 .env
# Edit .env and all service templates for this host.
sudo install -m 644 mikrotik-report-collect.service.example /etc/systemd/system/mikrotik-report-collect.service
sudo install -m 644 mikrotik-report-collect.timer.example /etc/systemd/system/mikrotik-report-collect.timer
sudo install -m 644 mikrotik-report-weekly.service.example /etc/systemd/system/mikrotik-report-weekly.service
sudo install -m 644 mikrotik-report-weekly.timer.example /etc/systemd/system/mikrotik-report-weekly.timer
sudo install -m 644 mikrotik-report-monthly.service.example /etc/systemd/system/mikrotik-report-monthly.service
sudo install -m 644 mikrotik-report-monthly.timer.example /etc/systemd/system/mikrotik-report-monthly.timer
sudo systemctl daemon-reload
sudo systemctl enable --now mikrotik-report-collect.timer mikrotik-report-weekly.timer mikrotik-report-monthly.timer
```

For an existing installation where the weekly unit runs as `root`, first grant
the weekly service SMTP password access through the group described above. Then update the installed weekly unit to
match the template while preserving its local user and paths, and run
`sudo systemctl daemon-reload`. The collector unit and database ownership stay
with the collector user.

The collector timer first runs two minutes after boot, then five minutes after
each activation. The weekly timer checks at 00:15 and the monthly timer at 00:30
every day in the host's local timezone. Both have `Persistent=true` to catch
missed runs after downtime and send only completed periods. Daily checks retry
pending mail after a transport failure. Align the host timezone and
`MIKROTIK_REPORT_TIMEZONE` if the first check should occur soon after a period
closes. `MIKROTIK_MONTHLY_SUBJECT` optionally sets the monthly email subject;
the recipient and mail account are shared with the weekly report.

For a manual run, use the same environment and user as the service:

```bash
sudo systemctl start mikrotik-report-collect.service
sudo systemctl start mikrotik-report-weekly.service
sudo systemctl start mikrotik-report-monthly.service
sudo journalctl -u mikrotik-report-collect.service -u mikrotik-report-weekly.service -u mikrotik-report-monthly.service -n 100
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

To inspect an arbitrary historical interval, run `range` as a user that can read
the configured database:

```bash
cd /path/to/self-hosted/mikrotik-report
MIKROTIK_REPORT_DB=/var/lib/mikrotik-report/report.sqlite3 \
MIKROTIK_REPORT_TIMEZONE=Europe/Madrid \
  python3 mikrotik_report.py range --from 2026-09-16 --to 2026-10-03
```

`--from` is inclusive and `--to` is exclusive. Both are local-calendar dates in
`MIKROTIK_REPORT_TIMEZONE`, so the example covers September 16 at 00:00 through
October 3 at 00:00 in that timezone. The command reads persisted daily
aggregates and writes only to stdout; it does not contact RouterOS, modify the
database, or send mail. Consequently, only `MIKROTIK_REPORT_DB` and optionally
`MIKROTIK_REPORT_TIMEZONE` are needed when invoking it outside the full service
environment.

Range totals include samples persisted inside the requested dates, including
ranges that cross weekly or monthly boundaries. Address-list maxima cover all
sampled days and the latest size comes from the last sampled day. The quality
block compares observed samples with the nominal five-minute cadence over the
exact interval. Partial coverage is marked explicitly and totals then describe
only observed samples; a range with no persisted samples reports activity as
unavailable rather than zero. Data from before daily aggregate collection was
introduced cannot be reconstructed from current RouterOS counters.

The first collection establishes a baseline; it does not claim traffic that
occurred before installation. Each later sample adds the difference from the
previous value. A lower counter or a detected router reboot starts a new
counter sequence. A newly observed rule starts with a fresh baseline to avoid
counting traffic that may have occurred before the collector saw it. If a rule
disappears, its last baseline is discarded. The report includes observed
router reboots, other counter resets, and new or recreated rule baselines.

Weekly boundaries use `MIKROTIK_REPORT_TIMEZONE`: Monday 00:00 is inclusive and
the following Monday 00:00 is exclusive. Monthly boundaries use day 1 at 00:00
inclusive through day 1 of the next month at 00:00 exclusive, in the same
timezone. Each counter difference is credited to the local day, week, and month
in which its later sample occurs. Monthly totals sum daily aggregates, so a
week crossing a month boundary is split correctly. Polling gaps, rules
created and removed between samples, and a reset followed by a counter that
already exceeds the old value without a detectable reboot can undercount or
hide a reset. They cannot be reconstructed from periodic counters. The report's
data-quality block shows observed samples, approximately expected samples and
coverage, router reboots, counter resets, and rule rebaselines. Expected samples
use a nominal five-minute cadence and the actual duration of the calendar period
in the configured timezone, including daylight-saving changes. Collection is
not fixed to an exact wall-clock grid, so the percentage is an estimate and can
slightly exceed 100% after manual collections. A week with no samples is not
evidence of zero blocked traffic.

The weekly email compares local and CrowdSec packet and byte totals, and the
latest and observed maximum size of each address list, with the immediately
preceding calendar week. Each comparison shows current and previous values,
absolute and percentage changes, and direction. Percentage change is unavailable
when the previous value is zero. A compact four-week trend lists traffic totals,
latest list sizes, and coverage, with missing weeks shown explicitly. Both weeks
must have at least 90% of the nominal sample count for a comparison; lower
coverage is shown as unavailable rather than as zero. The trend applies the same
rule. Router reboot, counter reset, and rule rebaseline counts are informational,
not traffic metrics. Historical comparisons start becoming available after the
first completed week has been emailed with sufficient coverage. Earlier reports
are not reconstructed from current router counters.

The monthly email shows local and CrowdSec packet and byte totals, latest and
maximum observed address-list sizes, and the same data-quality fields as the
weekly email. Month-over-month comparisons use the same current, previous,
absolute change, percentage, and direction rules. Both months need at least
90% estimated sample coverage; a missing or low-coverage previous month is
shown as unavailable. An entirely unsampled month has unavailable activity
values, not zero traffic. The first month after upgrading may have low coverage
because earlier collections did not create daily aggregates. The monthly command
queues each completed month since the first daily aggregate, including months
with no samples, and records successful delivery so a later daily timer run does
not resend it.

SQLite transactions serialize collection and reporting, and state survives
host restarts. A completed week or month stays pending if email delivery fails;
the corresponding report command exits nonzero and the next timer run retries.
A weekly aggregate enters the bounded history only after successful delivery.
If the process crashes after the mailer accepts a message but before SQLite
records success, the next run can send that period again. No additional SMTP
mechanism is used.

To recover, fix the router connection or mail configuration and restart the
failed service. Back up the SQLite file with SQLite's backup API or while all
timers are stopped; do not copy it during a write. To reset all reporting
history, stop all timers, move the database aside, and start them again. The
next collection will establish a new baseline.

## Code organization

`mikrotik_report.py` is a compatibility entry point kept stable for the systemd
units. The implementation lives in the `mikrotik_reporting` package:

* `config.py` validates command-specific environment configuration;
* `routeros.py` reads and validates RouterOS REST responses;
* `models.py` and `aggregation.py` define report data, calendar windows,
  counter deltas, and coverage;
* `storage.py` owns the SQLite schema, migrations, and queries;
* `rendering.py` produces report text without external side effects;
* `workflows.py` coordinates transactions, collection, and direct invocation of
  the shared `mail-notifier/send-mail.sh` transport;
* `cli.py` maps the four existing commands to those workflows.

Keep business decisions out of the CLI and SQLite helpers. New report formats
should consume aggregates through `rendering.py`; new collection data should be
normalized by `routeros.py` before it reaches aggregation or persistence.

## Local validation

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q mikrotik_report.py mikrotik_reporting
```

The tests use synthetic RouterOS responses and temporary SQLite files. They
do not contact the router or send email. This repository change does not enable
RouterOS `www-ssl`, create an account, or install systemd units on a live host.

RouterOS REST behavior and rule counters are documented by [MikroTik REST API](https://manual.mikrotik.com/docs/developer-guides/rest-api/)
and [MikroTik firewall matchers](https://manual.mikrotik.com/docs/firewall-and-quality-of-service/firewall/common-firewall-matchers-and-actions/).
The user group policies and address restriction are documented by [MikroTik User](https://manual.mikrotik.com/docs/authentication-authorization-accounting/user/).
