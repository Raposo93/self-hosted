# HTTP Endpoint Monitor

A small Bash script that monitors HTTP endpoints, keeps track of consecutive failures, and sends email notifications when an endpoint goes down or recovers.

Each endpoint has its own state and lock files, so the same script can monitor multiple services independently.

## Features

* Monitors HTTP and HTTPS endpoints.
* Considers `2xx` and `3xx` responses successful.
* Follows HTTP redirects.
* Detects HTTP errors and connection failures.
* Requires multiple consecutive failures before reporting downtime.
* Sends notifications when an endpoint goes down or recovers, retrying failed
  notifier calls at a bounded interval.
* Uses separate state and lock files for each endpoint.
* Writes all checks to a shared log with the endpoint name.
* Prevents overlapping checks for the same endpoint.

## Requirements

The following commands must be available:

* `bash`
* `curl`
* `flock`
* `logger`
* `mktemp`
* `msmtp`, required by `mail-notifier/send-mail.sh`, with the account
  passed to `--account` already configured
* permission to read the shared SMTP password; for a non-root cron user, follow
  the [mail notifier group setup](../mail-notifier/README.md#allow-a-non-root-service-to-send)

The monitor also requires the shared mail notifier to exist and be executable:

```text
mail-notifier/send-mail.sh
```

Both directories must share the same parent directory. The following structure is required:

```text
repository/
├── http-endpoint-monitor/
│   └── http-endpoint-monitor.sh
└── mail-notifier/
    └── send-mail.sh
```

## Configuration

Edit the constants near the beginning of the script:

```bash
FAIL_LIMIT=3
CONNECT_TIMEOUT=5
MAX_TIME=15
RETRY_INTERVAL=300
```

## Usage

For help:

```bash
./http-endpoint-monitor.sh --help
```

Run a monitor with:

```bash
./http-endpoint-monitor.sh \
    --name NAME \
    --url URL \
    --account MSMTP_ACCOUNT \
    --from SENDER_ADDRESS \
    --email-alert RECIPIENT_ADDRESS
```

Example:

```bash
./http-endpoint-monitor.sh \
    --name service-a \
    --url https://service-a.example.com \
    --account notifications \
    --from alerts@example.com \
    --email-alert admin@example.com
```

The monitor name may only contain:

```text
a-z A-Z 0-9 . _ -
```

The URL must start with either:

```text
http://
https://
```

## Monitoring multiple endpoints

Run the script once for each endpoint.

Example:

```bash
./http-endpoint-monitor.sh \
    --name service-a \
    --url https://service-a.example.com \
    --account notifications \
    --from alerts@example.com \
    --email-alert admin@example.com

./http-endpoint-monitor.sh \
    --name service-b \
    --url https://service-b.example.com \
    --account notifications \
    --from alerts@example.com \
    --email-alert admin@example.com
```

Each endpoint keeps an independent failure count and state.

## Cron example

Run two checks every five minutes:

```cron
*/5 * * * * /path/to/http-endpoint-monitor.sh --name service-a --url https://service-a.example.com --account notifications --from alerts@example.com --email-alert admin@example.com
*/5 * * * * /path/to/http-endpoint-monitor.sh --name service-b --url https://service-b.example.com --account notifications --from alerts@example.com --email-alert admin@example.com
```

Use absolute paths when running the script from cron.

## State transitions

The script starts with an `UNKNOWN` state.

A successful check stores:

```text
UP|0
```

A failed check increases the failure counter.

After reaching `FAIL_LIMIT`, the endpoint is stored as down:

```text
DOWN|3
```

Additional failures keep increasing the counter. If the downtime email was not
accepted by the notifier, it remains pending and is retried after
`RETRY_INTERVAL` seconds. A successful notifier call stops retries for that alert.

When a successful response is received after the endpoint was marked as down, the script sends a recovery notification and resets the state to:

```text
UP|0
```

Temporary failures below `FAIL_LIMIT` do not generate downtime or recovery notifications.

Alert delivery is tracked separately in `state/<name>.alert` as
`last_accepted|pending|last_attempt_epoch`. `last_accepted` is `UNKNOWN`, `DOWN`,
or `UP`; `pending` is `NONE`, `DOWN`, or `UP`. The attempt time is recorded before
calling the notifier, so an interrupted attempt is retried only after the
interval. The per-monitor lock also covers the delivery attempt, preventing
simultaneous sends for one monitor.

If the endpoint recovers before its downtime email is accepted, the pending
downtime alert is discarded and no recovery email is sent. If a recovery email
is pending and the endpoint goes down again, that obsolete recovery is
discarded; the already accepted downtime alert still describes the state known
to the recipient. A later recovery is announced normally.

The script only records an alert as accepted when `send-mail.sh` exits with
status `0`. This confirms acceptance by the local mail transport, not delivery
to a mailbox or that anyone read the message. A crash after transport acceptance
but before saving the result can cause a duplicate retry.
On an existing installation without an `.alert` file, delivery history is
unknown. The first check of an already down endpoint may send one additional
downtime email.

## Success criteria

A check is successful when:

* `curl` finishes successfully; and
* the HTTP response code is in the `2xx` or `3xx` range.

Examples:

```text
HTTP 200 → success
HTTP 302 → success
HTTP 404 → failure
HTTP 502 → failure
DNS error → failure
Connection timeout → failure
TLS certificate error → failure
```

## Files

The script creates a shared log file:

```text
http-endpoint-monitor.log
```

It also creates a `state` directory containing independent state and lock files:

```text
state/
├── service-a.state
├── service-a.alert
├── service-a.lock
├── service-b.state
├── service-b.alert
└── service-b.lock
```

The endpoint name is included in every log entry:

```text
2026-07-21 14:05:01 [service-a] OK http=200 tiempo=0.107368s
2026-07-21 14:05:01 [service-b] FALLO contador=1/3 http=502 curl=0 tiempo=1.086329s
```

## Exit codes

```text
0  Check completed, including pending alerts not yet due for retry; or another
   check for the same endpoint is already running
1  Mail notifier missing, not executable, or returned an error during an attempt;
   also used for invalid or unwritable alert state
2  Invalid or missing command-line arguments
```

HTTP and connection failures are recorded in the state and log files; they do
not themselves cause a non-zero exit code. A failed email attempt returns `1`
while leaving the observed HTTP state and failure counter intact. The log
distinguishes pending alerts, failed sends, and notifier acceptance.

## Email notifications

The script creates notifications on state transitions and retries them while
they still describe the current observed state:

* `UP` or `UNKNOWN` to `DOWN`
* `DOWN` to `UP`

A single temporary failure does not send an email unless it reaches the configured failure limit.

The recipient address, sender address, and msmtp account are required.

The script delegates email delivery to `mail-notifier/send-mail.sh`, and the
selected msmtp account must already be configured. If the monitor runs as a
non-root user, that user must have access to the SMTP password through the
`mail-notifier` group or an equivalent local permission setup.

Alerts are also written to the system log using:

```bash
logger -p user.warning -t http-monitor
```
