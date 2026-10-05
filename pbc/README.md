# Proxmox Backup Client

Reusable backup helper using Proxmox Backup Client, encrypted systemd credentials and email notifications.

Each backup profile uses its own environment file and can be scheduled with a systemd timer.

## Files

| File | Purpose |
| --- | --- |
| `pbc_backup_data.py` | Runs the backup and reports its overall result |
| `pbc_backup_data.sh` | Compatibility launcher for existing manual calls |
| `.env.example` | Example profile configuration |
| `pbc-backup@.service.example` | systemd service template |
| `pbc-backup-daily@.timer.example` | Fixed daily schedule |
| `pbc-backup-uptime@.timer.example` | Backup after boot and periodically |
| `pbc-backup-encryption.conf.example` | Optional systemd drop-in for encrypted profiles |

## Requirements

* Proxmox Backup Client
* Python 3.10 or newer
* `findmnt` (util-linux) when mount checks or included mounts are configured
* Access to a Proxmox Backup Server datastore
* PBS user or API token with backup permissions
* `systemd-creds`
* `msmtp` configured for email notifications
* `curl` when optional healthcheck monitoring is configured

## Create a backup profile

Copy the example using a profile name:

```bash
cp .env.example .env.example-profile
```

Example:

```bash
LOGFILE="/var/log/pbc/example-profile.log"
SOURCE_DIR="/path/to/data"
REPO="user@realm!api_token_name@host:datastore"
BACKUP_NAME="data.pxar"
BACKUP_ID="example-profile"

RECIPIENT_EMAIL="recipient@example.com"
SENDER_EMAIL="sender@example.com"
MSMTP_ACCOUNT="default"
```

Profile files such as `.env.example-profile` or `.env.ssh` must not be committed.

`BACKUP_ID` identifies this profile in PBS. With this example, backups belong
to `host/example-profile`. Use a different ID for each profile targeting the
same datastore and namespace, even when their archive names or sentinel files
are the same. The ID must start with a letter or digit and may contain only
letters, digits, `_`, `.` and `-`. An explicitly empty or invalid ID fails
before the client runs. If `BACKUP_ID` is omitted, the script passes no
`--backup-id` option and PBC continues to use its default hostname. Omission
does **not** isolate profiles from each other.

Set `NAMESPACE` only if this profile should back up into a PBS datastore
namespace. For example, `NAMESPACE="application-backups"` passes
`--ns application-backups` to the client. Leaving it unset or empty uses the
datastore's default namespace, as before. Create the namespace and grant the
token access to it in PBS before using this option.

### Mounted sources and included mounts

For a disk or network share, set `EXPECTED_MOUNT` to the mount point containing
`SOURCE_DIR`. The source may be a directory below it. The script resolves both
paths and checks that `findmnt -T SOURCE_DIR` reports that exact mount point;
a leftover local directory under an unmounted disk or share fails before the
client starts. Leave it unset for an ordinary local directory. To also verify
the mounted filesystem, set `EXPECTED_MOUNT_SOURCE` to the exact `SOURCE` value
shown by `findmnt -T /path/to/data -n -o SOURCE` on the intended mount. For an
NFS share, this is typically `server:/export`; for a local disk, device names
may change, so confirm the stable value on the host. A source identity setting
requires `EXPECTED_MOUNT`.

Proxmox Backup Client skips other mount points inside `SOURCE_DIR` by default.
Set `INCLUDE_DEV_MOUNTS` to a `|` separated list of absolute mount paths below
`SOURCE_DIR` to include only those mounts. Each path must be mounted when the
backup starts; spaces in paths are supported, but `|` is reserved as the
separator. The script passes each path as a separate `--include-dev` argument.
For example:

```bash
EXPECTED_MOUNT="/mnt/archive"
EXPECTED_MOUNT_SOURCE="nas:/archive"
SOURCE_DIR="/mnt/archive/data"
INCLUDE_DEV_MOUNTS="/mnt/archive/data/photos|/mnt/archive/data/media"
```

For profiles with mount dependencies, add a profile-specific systemd drop-in
such as `/etc/systemd/system/pbc-backup@<profile>.service.d/mount.conf`:

```ini
[Unit]
RequiresMountsFor=/mnt/archive
```

Use `Requires=` and `After=` for a specific network mount unit if systemd does
not manage that mount through the local mount table. Reload systemd after adding
the drop-in. The script still performs its own check so a missing or wrong
mount cannot start a backup if the unit dependency is ineffective.

### Transition for existing profiles

Before setting `BACKUP_ID` on an existing profile, record its current PBS group
(`host/<backup-hostname>`) and decide on a distinct ID for every profile that
shares a datastore and namespace. Set `BACKUP_ID` in each backup profile and
set the matching `RESTORE_GROUP="host/<backup-id>"` in its restore-check
profile. Confirm the first new backup and a successful sentinel restore before
relying on the new group. Review PBS permissions and ownership for the new
groups, plus any group-specific retention or prune rules; old and new groups
have separate histories. The script does not move or delete old snapshots.
They remain restorable by selecting the old `host/<backup-hostname>/<time>`
snapshot and its archive name with the credentials and encryption key used for
those backups. To keep checking the old group during the transition, use a
separate restore-check profile with its original `RESTORE_GROUP`.

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

For example, the `example-profile` profile uses:

```text
.env.example-profile
example-profile-api-token.cred
example-profile-fingerprint.cred
pbc-backup@example-profile.service
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

Replace `/path/to/self-hosted/pbc` with the real repository path. An installed
service using the old Bash `ExecStart` can continue through the small launcher,
but update the installed unit to invoke `pbc_backup_data.py` as shown, then run
`systemctl daemon-reload`. The `.env.<profile>` files and encrypted systemd
credentials do not change. The next scheduled run uses the updated service
after the reload.

Reload systemd:

```bash
sudo systemctl daemon-reload
```

A profile is selected through the instance name:

```text
pbc-backup@example-profile.service -> .env.example-profile
pbc-backup@example-profile.service    -> .env.ssh
```

Test a profile manually:

```bash
sudo systemctl start pbc-backup@example-profile.service
sudo systemctl status pbc-backup@example-profile.service
```

View logs:

```bash
journalctl -u pbc-backup@example-profile.service
```

## Scheduling

### Optional missed-run monitoring

Set `HEALTHCHECK_URL` in a private profile file to a Healthchecks-compatible
ping URL, for example `https://monitor.example.com/ping/your-private-id`.
Immediately before PBC starts, the script sends a ping to `<url>/start`.
After PBC and any post-hook finish, it pings `<url>` only when the entire run
succeeds or `<url>/fail` if preparation, PBC, cleanup, or validation fails or
the run is cancelled. A failed pre-hook sends a failure ping even though PBC
was not started; there is no start ping in that case.
The monitoring service must be configured with an expected period and grace
time matching the profile's timer to detect missed runs. No requests are sent
when the URL is unset or empty. These pings complement the existing email
notification; an unavailable monitoring service produces a warning in the
backup log and never changes the backup exit status. Each request has a
10-second deadline. Keep the URL private because it contains a check ID.

### Optional preparation and cleanup hooks

Set `PRE_BACKUP_HOOK` and/or `POST_BACKUP_HOOK` in a private profile to
executable files, using absolute paths. For example, an application profile
can run a pre-hook that writes a fresh logical database dump into `SOURCE_DIR`
and a post-hook that removes that temporary dump after the backup attempt.
Keep the dump and hook scripts outside Git if they contain private data or
credentials. Hook output and exit codes go to `LOGFILE` and the existing email.

The pre-hook runs before PBC. If it fails, PBC and the post-hook do not run;
the service fails and sends a failure email and, when configured, a failure
ping. A partially completed pre-hook must handle its own rollback. After a
successful pre-hook (or when none is configured), the post-hook runs after the
PBC attempt even if PBC fails. A configured hook that is missing or not
executable fails the run before preparation or backup starts and is reported
as a configuration error. If PBC and the post-hook both fail, the service
returns the PBC exit code and reports both failures. If only the post-hook
fails, the service returns its exit code and the final healthcheck ping fails.

The subject and exit status describe the whole run:

| Result | Email subject prefix | Final ping | Exit status |
| --- | --- | --- | --- |
| All phases succeed | `[OK] Backup completed:` | Success | `0` |
| PBC succeeds; post-hook fails | `[WARN] Backup completed; post-hook failed:` | Failure | Post-hook code |
| PBC fails | `[ERROR] Backup failed:` | Failure | PBC code |
| Pre-hook fails before PBC | `[ERROR] Backup not started; pre-hook failed:` | Failure | Pre-hook code |
| Cancelled | `[ERROR] Backup cancelled:` | Failure | `130` or `143` |
| Invalid configuration | `[ERROR] Backup not started; configuration error:` | Failure | Non-zero |

The subject continues with `<archive> on <host>`. The email body records which
phases ran, their exit codes, and the overall exit code. Monitor and mail
failures are logged as warnings; they do not override the backup result or
prevent the post-hook from running.

On SIGTERM or SIGINT during a backup, the script requests client termination,
waits for it to exit, and then runs the post-hook once if the pre-hook succeeded.
Cancellation returns a non-zero status and is reported as a failure, including
when the post-hook also fails. The example systemd service uses
`KillMode=control-group`, so systemd signals the script and its running
client together. `TimeoutStopSec=5min` gives the client and post-hook time to
finish before systemd forces termination; adjust it for profiles whose cleanup
needs longer. A client that leaves detached processes may need its own stop
handling before a post-hook can safely remove data they use. SIGKILL, power
loss, and an interrupted pre-hook cannot guarantee recovery; a partially run
pre-hook does not trigger the post-hook unless it completed successfully.

### Fixed daily schedule

Suitable for always-on systems.

```bash
sudo cp pbc-backup-daily@.timer.example \
  /etc/systemd/system/pbc-backup-daily@.timer

sudo systemctl daemon-reload
sudo systemctl enable --now pbc-backup-daily@example-profile.timer
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
sudo systemctl enable --now pbc-backup-uptime@example-profile.timer
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

* validates the profile variables, including any explicit `BACKUP_ID`
* checks that `SOURCE_DIR` exists and, when configured, verifies its mount and source
* includes only the internal mounts named by `INCLUDE_DEV_MOUNTS`
* uses PBS credentials loaded by the systemd service
* runs `proxmox-backup-client backup`
* optionally runs preparation and cleanup hooks around PBC
* optionally reports backup start and the overall run result to a healthcheck endpoint
* writes the configured log file
* sends an email notification
* exits with the cancellation, pre-hook, backup, or post-hook status in that order

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

For scheduled file restore verification, see
[`pbc-restore-check/`](../pbc-restore-check/README.md). It restores and validates
a sentinel from the latest snapshot using the same PBS credentials.

Encrypted backups require the profile encryption key and its password to restore their contents.

Keep an offline copy of the encryption key and password outside the machine being backed up. Do not keep the only recovery material on the protected machine.

Periodically test restoring an encrypted backup to a temporary directory:

```bash
proxmox-backup-client restore <snapshot> <archive-name> /tmp/pbc-restore-test \
  --repository "<repository>" \
  --keyfile /path/to/<profile>-encryption-key.json
```
