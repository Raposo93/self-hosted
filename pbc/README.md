# Proxmox Backup Client

Backup helper script using Proxmox Backup Client, encrypted systemd credentials and email notifications.

This directory contains a reusable script for backing up a local directory to a Proxmox Backup Server datastore.

## Files

| File | Purpose |
| --- | --- |
| `pbc_backup_data.sh` | Runs the backup with Proxmox Backup Client |
| `sample.env` | Example environment configuration |
| `.env` | Local configuration file, not committed |

## Requirements

- Proxmox Backup Client installed on the machine running the backup
- Access to a Proxmox Backup Server datastore
- A PBS user or API token with backup permissions
- `systemd-creds`
- `msmtp` configured for email notifications

## Setup

Copy the sample environment file:

```bash
cp sample.env .env
```

Edit `.env` with the local values:

```bash
LOGFILE="/var/log/pbc/backup.log"
SOURCE_DIR="/path/to/data"
REPO="user@realm!api_token_name@host:datastore"
BACKUP_NAME="data.pxar"

PBS_PASSWORD_CRED="/root/.config/proxmox-backup/my-api-token.cred"
PBS_FINGERPRINT_CRED="/root/.config/proxmox-backup/my-fingerprint.cred"

RECIPIENT_EMAIL="recipient@example.com"
SENDER_EMAIL="sender@example.com"
MSMTP_ACCOUNT="default"
```

The `.env` file must not be committed.

## Repository format

The `REPO` value uses this format:

```text
user@realm!api_token_name@host:datastore
```

Example:

```text
photos@pbs!photos-backup@10.1.1.22:photos-backup
```

If you type the repository directly in an interactive shell, escape `!` or disable Bash history expansion:

```bash
set +H
```

This is not needed when the value is loaded from `.env` and used as `"$REPO"` inside the script.

## Create encrypted credentials

Enter a root shell:

```bash
sudo -i
```

Create the credential directory:

```bash
install -d -m 700 -o root -g root /root/.config/proxmox-backup
```

Create the encrypted API token secret credential:

```bash
systemd-ask-password -n "PBS API token secret: " \
  | systemd-creds encrypt \
      --name=proxmox-backup-client.password \
      - \
      /root/.config/proxmox-backup/my-api-token.cred
```

Create the encrypted fingerprint credential:

```bash
systemd-ask-password -n "PBS fingerprint: " \
  | systemd-creds encrypt \
      --name=proxmox-backup-client.fingerprint \
      - \
      /root/.config/proxmox-backup/my-fingerprint.cred
```


Lock down permissions:

```bash
chown -R root:root /root/.config/proxmox-backup
chmod 700 /root/.config/proxmox-backup
chmod 600 /root/.config/proxmox-backup/*.cred
```

Expected result:

```text
drwx------ 2 root root ... /root/.config/proxmox-backup
-rw------- 1 root root ... my-api-token.cred
-rw------- 1 root root ... my-fingerprint.cred
```

## PBS permissions

The API token needs permission on the target datastore.

For a backup-only token, assign:

```text
Path: /datastore/<datastore-name>
Role: DatastoreBackup
```

Example:

```text
Path: /datastore/photos-backup
User/Token: photos@pbs!photos-backup
Role: DatastoreBackup
Propagate: yes
```

Without the correct datastore permission, the client may fail with:

```text
Error: permission check failed
```

## Test the credentials

Run this as root:

```bash
systemd-run \
  --pipe --wait --collect \
  --property=LoadCredentialEncrypted=proxmox-backup-client.password:/root/.config/proxmox-backup/my-api-token.cred \
  --property=LoadCredentialEncrypted=proxmox-backup-client.fingerprint:/root/.config/proxmox-backup/my-fingerprint.cred \
  proxmox-backup-client status \
    --repository "photos@pbs!photos-backup@10.1.1.22:photos-backup"
```

A successful response shows datastore usage and ends with:

```text
Finished with result: success
```

## Test email notifications

Load the local environment file:

```bash
set -a
source .env
set +a
```

Send a test email:

```bash
msmtp -a "$MSMTP_ACCOUNT" "$RECIPIENT_EMAIL" <<EOF
From: $SENDER_EMAIL
To: $RECIPIENT_EMAIL
Subject: Test backup notification from $(hostname)

This is a test email using the same settings as pbc_backup_data.sh.
EOF
```

If it fails, run with debug output:

```bash
msmtp --debug -a "$MSMTP_ACCOUNT" "$RECIPIENT_EMAIL"
```

## Run the backup

Make the script executable:

```bash
chmod +x pbc_backup_data.sh
```

Run it:

```bash
./pbc_backup_data.sh
```

The script will:

- load `.env`
- validate required variables
- check that `SOURCE_DIR` exists
- run `proxmox-backup-client backup`
- write a log file
- send an email notification
- exit with the backup command status

## Security notes

Do not commit:

- `.env`
- `.cred` files
- API token secrets
- logs
- generated backup output

Recommended `.gitignore` entries:

```gitignore
.env
*.cred
*.log
```

The encrypted credential files are still treated as sensitive and should remain owned by root with restrictive permissions.

## Troubleshooting

### `No such file or directory: .env`

Run the command from this directory or source the environment file with an absolute path.

Example:

```bash
source /home/gonzalo/self-hosted/pbc/.env
```

### `msmtp: no recipients found`

The environment file was not loaded or `RECIPIENT_EMAIL` is empty.

Check:

```bash
echo "$RECIPIENT_EMAIL"
echo "$MSMTP_ACCOUNT"
echo "$SENDER_EMAIL"
```

### `event not found`

Bash is interpreting `!` in the repository string.

Use:

```bash
set +H
```

or escape the exclamation mark when typing the repository manually:

```bash
photos@pbs\!photos-backup@10.1.1.22:photos-backup
```

### `Error: permission check failed`

The API token does not have the required permission on the datastore.

Assign `DatastoreBackup` on:

```text
/datastore/<datastore-name>
```

### `Credential secret file is not located on encrypted media`

This warning means systemd's local credential secret is not stored on encrypted media. The credential file is still generated, but the host should be treated as trusted.
