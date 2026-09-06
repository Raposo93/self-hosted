# Proxmox Backup Client

Reusable backup helper using Proxmox Backup Client, encrypted systemd credentials and email notifications.

Each backup profile uses its own environment file and can be scheduled with a systemd timer.

## Files

| File | Purpose |
| --- | --- |
| `pbc_backup_data.sh` | Runs the backup |
| `.env.example` | Example profile configuration |
| `pbc-backup@.service.example` | systemd service template |
| `pbc-backup-daily@.timer.example` | Fixed daily schedule |
| `pbc-backup-uptime@.timer.example` | Backup after boot and periodically |
| `pbc-backup-encryption.conf.example` | Optional systemd drop-in for encrypted profiles |

## Requirements

* Proxmox Backup Client
* Access to a Proxmox Backup Server datastore
* PBS user or API token with backup permissions
* `systemd-creds`
* `msmtp` configured for email notifications

## Create a backup profile

Copy the example using a profile name:

```bash
cp .env.example .env.photos
```

Example:

```bash
LOGFILE="/var/log/pbc/photos.log"
SOURCE_DIR="/path/to/data"
REPO="user@realm!api_token_name@host:datastore"
BACKUP_NAME="data.pxar"

RECIPIENT_EMAIL="recipient@example.com"
SENDER_EMAIL="sender@example.com"
MSMTP_ACCOUNT="default"
```

Profile files such as `.env.photos` or `.env.ssh` must not be committed.

Repository format:

```text
user@realm!api_token_name@host:datastore
```

## Create encrypted credentials

Create the credential directory:

```bash
sudo install -d -m 700 -o root -g root /root/.config/proxmox-backup
```

Create the API token credential:

```bash
sudo systemd-ask-password -n "PBS API token secret: " \
  | sudo systemd-creds encrypt \
      --name=proxmox-backup-client.password \
      - \
      /root/.config/proxmox-backup/<profile>-api-token.cred
```

Create the fingerprint credential:

```bash
sudo systemd-ask-password -n "PBS fingerprint: " \
  | sudo systemd-creds encrypt \
      --name=proxmox-backup-client.fingerprint \
      - \
      /root/.config/proxmox-backup/<profile>-fingerprint.cred
```

For example, the `photos` profile uses:

```text
.env.photos
photos-api-token.cred
photos-fingerprint.cred
pbc-backup@photos.service
```

Protect the files:

```bash
sudo chmod 700 /root/.config/proxmox-backup
sudo chmod 600 /root/.config/proxmox-backup/*.cred
```

## PBS permissions

The API token needs `DatastoreBackup` on the target datastore:

```text
Path: /datastore/<datastore-name>
Role: DatastoreBackup
```

## Install the systemd service

Copy and edit the service template:

```bash
sudo cp pbc-backup@.service.example \
  /etc/systemd/system/pbc-backup@.service
```

Replace `/path/to/self-hosted/pbc` with the real repository path.

Reload systemd:

```bash
sudo systemctl daemon-reload
```

A profile is selected through the instance name:

```text
pbc-backup@photos.service -> .env.photos
pbc-backup@ssh.service    -> .env.ssh
```

Test a profile manually:

```bash
sudo systemctl start pbc-backup@photos.service
sudo systemctl status pbc-backup@photos.service
```

View logs:

```bash
journalctl -u pbc-backup@photos.service
```

## Scheduling

### Fixed daily schedule

Suitable for always-on systems.

```bash
sudo cp pbc-backup-daily@.timer.example \
  /etc/systemd/system/pbc-backup-daily@.timer

sudo systemctl daemon-reload
sudo systemctl enable --now pbc-backup-daily@photos.timer
```

Default schedule:

```text
03:00 daily
```

### Uptime-based schedule

Suitable for systems without a fixed uptime schedule.

```bash
sudo cp pbc-backup-uptime@.timer.example \
  /etc/systemd/system/pbc-backup-uptime@.timer

sudo systemctl daemon-reload
sudo systemctl enable --now pbc-backup-uptime@ssh.timer
```

Default behavior:

```text
2 minutes after boot
then every 12 hours while the system remains running
```

Check timers with:

```bash
systemctl list-timers 'pbc-backup*'
```

## What the script does

The script:

* validates the profile variables
* checks that `SOURCE_DIR` exists
* uses PBS credentials loaded by the systemd service
* runs `proxmox-backup-client backup`
* writes the configured log file
* sends an email notification
* exits with the backup command status

## Security

Do not commit:

```text
.env.*
*.cred
*.log
```

Keep `.env.example` as the only environment template tracked by Git.

### Optional client-side encryption

Set `ENCRYPTION_KEYFILE` in the profile `.env.<profile>` to enable client-side encryption.

Create the encryption key:

```bash
sudo proxmox-backup-client key create \
  /root/.config/proxmox-backup/<profile>-encryption-key.json
```

Create the matching encrypted password credential:

```bash
sudo systemd-ask-password -n "PBS encryption key password: " \
  | sudo systemd-creds encrypt \
      --name=proxmox-backup-client.encryption-password \
      - \
      /root/.config/proxmox-backup/<profile>-encryption-password.cred
```

Install the encryption drop-in for the profile:

```bash
sudo mkdir -p /etc/systemd/system/pbc-backup@<profile>.service.d

sudo cp pbc-backup-encryption.conf.example \
  /etc/systemd/system/pbc-backup@<profile>.service.d/encryption.conf

sudo systemctl daemon-reload
```

Profiles without `ENCRYPTION_KEYFILE` and without the encryption drop-in run unencrypted.

## Encryption recovery

Encrypted backups require the profile encryption key and its password to restore their contents.

Keep an offline copy of the encryption key and password outside the machine being backed up. Do not keep the only recovery material on the protected machine.

Periodically test restoring an encrypted backup to a temporary directory:

```bash
proxmox-backup-client restore <snapshot> <archive-name> /tmp/pbc-restore-test \
  --repository "<repository>" \
  --keyfile /path/to/<profile>-encryption-key.json
```
