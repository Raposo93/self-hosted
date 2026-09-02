# Spaceship DDNS

Dynamic DNS updater for domains managed through the Spaceship DNS API.

The script detects the current public IPv4 address, compares it with the
configured DNS `A` record and updates Spaceship only when necessary.

It also removes obsolete addresses left by previous IP changes.

## Requirements

- Python 3.10 or newer
- systemd
- Internet access
- Spaceship API credentials with:
  - `dnsrecords:read`
  - `dnsrecords:write`

No external Python dependencies are required.

## Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

Edit `.env` and set the required values:

```ini
SPACESHIP_API_KEY=your_api_key
SPACESHIP_API_SECRET=your_api_secret

DOMAIN=your_domain.net
RECORD_NAME=@
TTL=300
```

Protect the credentials:

```bash
chmod 600 .env
```

The `.env` file contains secrets and must not be committed.

## Manual test

Load the environment variables:

```bash
set -a
source .env
set +a
```

Run the updater:

```bash
python3 spaceship-ddns.py
```

If DNS is already correct:

```text
INFO: Current public IP: 203.0.113.10
INFO: Current Spaceship A records for example.net: 203.0.113.10
INFO: DNS already up to date
```

If the public IP has changed, the script first adds the new address and then
removes obsolete addresses.

This order avoids leaving the domain without an `A` record if an API request
fails during an update.

## systemd

A reusable timer can be stored directly in the repository.

The service file is provided as `spaceship-ddns.service.example` because its
user and filesystem paths depend on the host where it is installed.

Install the units directly from the repository:

```bash
sudo cp spaceship-ddns.service.example \
  /etc/systemd/system/spaceship-ddns.service

sudo cp spaceship-ddns.timer \
  /etc/systemd/system/spaceship-ddns.timer
```

Edit the installed service and replace the example user and paths with the
local values:

```bash
sudo nano /etc/systemd/system/spaceship-ddns.service
```

Reload systemd:

```bash
sudo systemctl daemon-reload
```

Test the service manually:

```bash
sudo systemctl start spaceship-ddns.service
```

Check its output:

```bash
journalctl -u spaceship-ddns.service
```

Enable and start the timer:

```bash
sudo systemctl enable --now spaceship-ddns.timer
```

Check the timer:

```bash
systemctl status spaceship-ddns.timer
systemctl list-timers spaceship-ddns.timer
```

## Logs

The script writes to standard output and error, so systemd stores its output
in the journal.

Recent executions:

```bash
journalctl -u spaceship-ddns.service -n 50
```

Follow executions live:

```bash
journalctl -u spaceship-ddns.service -f
```

## Disable

Stop and disable the timer:

```bash
sudo systemctl disable --now spaceship-ddns.timer
```

The oneshot service does not remain running between timer executions.

## Files

```text
spaceship-ddns/
├── .env.example
├── README.md
├── spaceship-ddns.py
├── spaceship-ddns.service.example
└── spaceship-ddns.timer
```

The `.env` file contains local credentials and should not be versioned.
