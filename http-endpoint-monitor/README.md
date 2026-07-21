# HTTP Endpoint Monitor

A small Bash script that monitors HTTP endpoints, keeps track of consecutive failures, and sends email notifications when an endpoint goes down or recovers.

Each endpoint has its own state and lock files, so the same script can monitor multiple services independently.

## Features

* Monitors HTTP and HTTPS endpoints.
* Considers `2xx` and `3xx` responses successful.
* Follows HTTP redirects.
* Detects HTTP errors and connection failures.
* Requires multiple consecutive failures before reporting downtime.
* Sends one notification when an endpoint goes down.
* Sends one notification when the endpoint recovers.
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
    --name navidrome \
    --url https://navidrome.example.com \
    --account mailjet \
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
    --name immich \
    --url https://photos.example.com \
    --account mailjet \
    --from alerts@example.com \
    --email-alert admin@example.com

./http-endpoint-monitor.sh \
    --name blueiris \
    --url https://cameras.example.com \
    --account mailjet \
    --from alerts@example.com \
    --email-alert admin@example.com
```

Each endpoint keeps an independent failure count and state.

## Cron example

Run two checks every five minutes:

```cron
*/5 * * * * /path/to/http-endpoint-monitor.sh --name immich --url https://photos.example.com --account mailjet --from alerts@example.com --email-alert admin@example.com
*/5 * * * * /path/to/http-endpoint-monitor.sh --name blueiris --url https://cameras.example.com --account mailjet --from alerts@example.com --email-alert admin@example.com
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

Additional failures keep increasing the counter, but no additional downtime notifications are sent.

When a successful response is received after the endpoint was marked as down, the script sends a recovery notification and resets the state to:

```text
UP|0
```

Temporary failures below `FAIL_LIMIT` do not generate downtime or recovery notifications.

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
├── immich.state
├── immich.lock
├── blueiris.state
└── blueiris.lock
```

The endpoint name is included in every log entry:

```text
2026-07-21 14:05:01 [immich] OK http=200 tiempo=0.107368s
2026-07-21 14:05:01 [blueiris] FALLO contador=1/3 http=502 curl=0 tiempo=1.086329s
```

## Exit codes

```text
0  Check completed, or another check for the same endpoint is already running
1  Shared mail notifier is missing or not executable
2  Invalid or missing command-line arguments
```

HTTP and connection failures are recorded in the state and log files. They do not currently cause the script itself to return a non-zero exit code.

## Email notifications

The script sends notifications only on state transitions:

* `UP` or `UNKNOWN` to `DOWN`
* `DOWN` to `UP`

A single temporary failure does not send an email unless it reaches the configured failure limit.

The recipient address, sender address, and msmtp account are required.

The script delegates email delivery to `mail-notifier/send-mail.sh`, and the
selected msmtp account must already be configured.

Alerts are also written to the system log using:

```bash
logger -p user.warning -t http-monitor
```
