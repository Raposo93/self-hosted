# Mail notifier

`send-mail.sh` sends plain-text emails through `msmtp`.

Email metadata is passed as arguments, while the message body is read from
standard input. It is intended for monitors, backups, and systemd tasks
running as `root`.

## Install msmtp

```bash
sudo apt update
sudo apt install msmtp ca-certificates
```

## Configure msmtp

Create the password file:

```bash
sudo install -d -o root -g root -m 700 /etc/msmtp

sudo install \
    -o root \
    -g root \
    -m 600 \
    /dev/null \
    /etc/msmtp/notifications.password

sudoedit /etc/msmtp/notifications.password
```

The file must contain only the SMTP password:

```text
SMTP_PASSWORD
```

Create `/etc/msmtprc`:

```bash
sudo install \
    -o root \
    -g root \
    -m 600 \
    /dev/null \
    /etc/msmtprc

sudoedit /etc/msmtprc
```

Generic configuration for STARTTLS on port `587`:

```text
defaults
auth on
tls on
tls_starttls on
tls_trust_file system
timeout 15
syslog LOG_MAIL

account notifications
host smtp.example.com
port 587
from notifications@example.com
user notifications@example.com
passwordeval "cat /etc/msmtp/notifications.password"

account default : notifications
```

Replace the server, user, and sender address with the values provided by
your SMTP provider.

For direct TLS on port `465`:

```text
port 465
tls_starttls off
```

## Test the configuration

```bash
{
    printf 'From: notifications@example.com\n'
    printf 'To: recipient@example.com\n'
    printf 'Subject: msmtp test\n'
    printf '\n'
    printf 'Test message.\n'
} | sudo msmtp \
    --pretend \
    --account=notifications \
    recipient@example.com
```

Remove `--pretend` to send the email.

View the logs:

```bash
sudo journalctl --since today --facility=mail
```

## Use send-mail.sh

```bash
chmod +x send-mail.sh
```

```text
send-mail.sh --to ADDRESS --subject SUBJECT [OPTIONS]

-a, --account NAME       msmtp account
-f, --from HEADER        optional From header
-t, --to ADDRESS         recipient address
-s, --subject SUBJECT    email subject
-h, --help               show this help
```

`--to` and `--subject` are required. The message body must be provided
through standard input.

The manual examples use `sudo` because only `root` can read the SMTP
password. Services already running as `root` do not need it.

### Send a variable

```bash
printf '%s\n' "$message" |
    sudo ./send-mail.sh \
        --account notifications \
        --from 'Service monitor <notifications@example.com>' \
        --to 'recipient@example.com' \
        --subject 'Service unavailable'
```

### Send a file

```bash
sudo ./send-mail.sh \
    --account notifications \
    --from 'Backup server <notifications@example.com>' \
    --to 'recipient@example.com' \
    --subject 'Backup completed' \
    < /var/log/backup.log
```

## Use it from another repository script

```bash
SCRIPT_DIR="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &&
    pwd
)"

REPO_DIR="$(dirname -- "$SCRIPT_DIR")"
SEND_MAIL="$REPO_DIR/mail-notifier/send-mail.sh"

if [[ ! -x "$SEND_MAIL" ]]; then
    echo "Error: Mail notifier not found or not executable: $SEND_MAIL" >&2
    exit 1
fi
```

Example:

```bash
if ! "$SEND_MAIL" \
    --account "$MSMTP_ACCOUNT" \
    --from "$SENDER_EMAIL" \
    --to "$RECIPIENT_EMAIL" \
    --subject "$SUBJECT" \
    < "$LOGFILE"
then
    log "Warning: Failed to send notification email"
fi
```

## Exit codes

* `0`: email sent successfully.
* `2`: invalid arguments or empty message body.
* Any other value: error returned by `msmtp`.

## Scope and security

The helper only sends plain-text messages. It does not support HTML,
attachments, CC, or BCC.

Do not store credentials in Git or include them directly in scripts.
Keep `/etc/msmtprc` and the password file with `600` permissions.
