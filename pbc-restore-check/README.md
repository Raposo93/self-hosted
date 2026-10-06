# PBC restore verification

Monthly sentinel restore checks for the file backups created by `pbc/`. This
invokes the real `proxmox-backup-client restore` command against PBS, using a
root sentinel filename as a selective restore pattern. Requires Python 3,
Proxmox Backup Client with `restore --pattern`, and the shared
[`mail-notifier`](../mail-notifier/README.md) configuration. No Python packages
are required. No production data is overwritten.

## Sentinel and profile

Create a regular file at the root of the backup profile's `SOURCE_DIR`:

```bash
printf 'pbc-restore-sentinel-v1\n' > /path/to/data/.pbc-restore-sentinel
```

Ensure it is included in a subsequent successful backup before enabling checks.
Do not create or modify the sentinel from this checker. Copy `.env.example` to
`.env.<profile>` here and set `REPO`, `BACKUP_NAME` and `RESTORE_GROUP` to match
that backup. Set the same `NAMESPACE` as the backup profile. For a backup
profile with `BACKUP_ID="example-profile"`, use
`RESTORE_GROUP="host/example-profile"`; this matches the supplied examples.
For a legacy backup profile without `BACKUP_ID`, use its existing
`host/<backup-hostname>` group and confirm it in PBS. `RESTORE_GROUP` is
required; an empty value fails. The checker filters JSON snapshot results by
this exact group and selects the greatest `backup-time`.
It never falls back to an older snapshot if the newest one cannot be restored.
Different profiles can use the same archive name and sentinel content safely
when their backup IDs/restore groups or namespaces are distinct. The checker
passes a configured namespace to both `snapshot list` and `restore`; a missing
snapshot in that namespace fails without trying the datastore root. If
`NAMESPACE` is omitted or empty, it selects the root namespace. Both backup
and verification ignore an inherited `PBS_NAMESPACE` in all cases, so `NAMESPACE` is the only
namespace setting for this profile and takes precedence when both are set.
See the
[backup profile transition](../pbc/README.md#transition-for-existing-profiles)
for old snapshots, permissions and retention considerations.

Use the same profile name and encrypted API token/fingerprint credentials as
[`pbc/`](../pbc/README.md). The authentication identity must be allowed to list
and read the selected backups: the backup owner can restore its own backups;
an independent restore identity needs the appropriate PBS read permissions
(such as `DatastoreReader`). Do not put passwords in tracked configuration.

Configuration:

* `NAMESPACE`: same optional datastore namespace as the backup profile; omit
  or leave empty for the root namespace.
* `RESTORE_TMP_BASE`: existing writable temporary base directory, with enough
  disk space; each run creates its own private directory.
* `MAX_SNAPSHOT_AGE_SECONDS`: maximum age; `0` disables the age limit.
* `RESTORE_TIMEOUT_SECONDS`: positive timeout per client command, default 3600.
* `SENTINEL_PATH`: literal filename at the archive root, default
  `.pbc-restore-sentinel`; nested paths and glob patterns are rejected.
* `SENTINEL_CONTENT`: expected UTF-8 text, with exactly one appended newline.
* `SENTINEL_SHA256`: optional SHA-256 of the complete file, including newline.
  Calculate it with `sha256sum /path/to/data/.pbc-restore-sentinel`.
* `ENCRYPTION_KEYFILE`: original encryption key for encrypted backups.
* `RECIPIENT_EMAIL`, `SENDER_EMAIL`, `MSMTP_ACCOUNT`: shared notifier metadata.

Encrypted profiles also need this instance-specific drop-in:

```ini
# /etc/systemd/system/pbc-restore-check@<profile>.service.d/encryption.conf
[Service]
LoadCredentialEncrypted=proxmox-backup-client.encryption-password:/root/.config/proxmox-backup/<profile>-encryption-password.cred
```

The key and password must both be supplied. Keep recovery material offline as
described in the PBC README. Interactive prompts are disabled for client calls.

## Install and operate

Edit repository paths in the service example, then install the templates:

```bash
sudo cp pbc-restore-check@.service.example /etc/systemd/system/pbc-restore-check@.service
sudo cp pbc-restore-check@.timer.example /etc/systemd/system/pbc-restore-check@.timer
sudo systemctl daemon-reload
sudo systemctl start pbc-restore-check@<profile>.service
sudo systemctl enable --now pbc-restore-check@<profile>.timer
```

The timer runs on the first day of each month at 06:00, with up to one hour of
random delay and catch-up after downtime. systemd prevents overlapping runs of
the same service instance. Different instances use independent temporary paths.
For manual checks prefer `systemctl start` so encrypted credentials are loaded.
For an already exported environment, run `bash pbc_restore_check.sh`; standard
PBC authentication environment variables also work for manual execution.

View results with `journalctl -u pbc-restore-check@<profile>.service` and timers
with `systemctl list-timers 'pbc-restore-check*'`. Generic client failure messages
avoid exposing authentication details; investigate PBS access manually when
necessary. A successful message identifies the namespace, snapshot and archive checked.

## Results, cleanup and limitations

Exit `0` means the sentinel was restored, its exact content (and optional hash)
matched, temporary data was removed, and the success email was sent. Exit `1`
means configuration, selection, restore, validation or cleanup failed. Missing
sentinels, symlinks, excessive age and timestamps over five minutes in the future
are failures. Success and failure emails use the shared notifier. Notification
failure is logged and returns `2` after a successful check; it preserves `1`
after a failed check.

Temporary data is removed on success, ordinary errors, timeouts, SIGINT and
SIGTERM. SIGKILL, power loss and filesystem cleanup failures cannot guarantee
removal; inspect leftover `pbc-restore-check-*` directories under the configured
base after an interrupted host and remove them only after confirming no check
is running. No restored content or client output is retained in logs or mail.
Keep the temporary base on trusted local storage.

This verifies the actual restore path for the selected sentinel, including
decryption when configured. It does not establish integrity of every file,
complete disaster recovery readiness, or VM/LXC bootability. The separate
`proxmox-restore-check/` utility handles VM/LXC drills.

Disable the timer to roll back scheduling; removing the checker does not alter
PBS backups. Remove the source sentinel only if you no longer need future checks.
The checker does not install units, run backups or modify live configuration.

Client syntax reference: [Proxmox Backup Client manual](https://pbs.proxmox.com/docs/proxmox-backup-client/man1.html).
