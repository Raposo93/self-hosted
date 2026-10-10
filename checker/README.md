# Daily host checker

A single `.env`, oneshot service and daily timer run independent security/reboot
and disk/inode checks on Debian, Ubuntu and Proxmox hosts. No packages are
installed by checks, files deleted, or hosts rebooted. Each check sends its own
attention emails through [mail-notifier](../mail-notifier/README.md).

## Dependencies and configuration

Requires Bash, `/usr/bin/python3`, util-linux (`flock`), GNU coreutils (`du`),
systemd, and the configured shared notifier. Security checks also require
`apt-get` and distribution `python3-apt`; see the existing
[security detection policy and limitations](../security-update-check/README.md).
That component remains the single source of APT and reboot detection logic.

On the target host, install dependencies and configure the root msmtp account
as described in the notifier README, then:

```bash
cp checker/.env.example checker/.env
chmod 600 checker/.env
# Edit checker/.env before installing.
sudo ./checker/install.sh
```

The checkout and `.env` must be writable only by trusted administrators: `.env`
is sourced as Bash by a root process. Installation validates configuration and
stages/verifies units before replacing them, reloads systemd, and enables only
`checker.timer`. It does not start a check immediately (a missed persistent timer
execution can run after activation). Re-running installs updated units without
replacing configuration or notification state. Checkout paths must contain only
letters, digits, underscores, dots, slashes and hyphens.

| Variable | Meaning |
| --- | --- |
| `HOST_NAME` | Email host label; defaults to hostname if omitted/empty |
| `RECIPIENT_EMAIL` | Required notification recipient |
| `MSMTP_ACCOUNT`, `SENDER_EMAIL` | Optional shared notifier account and sender |
| `SEND_MAIL` | Absolute executable notifier path; defaults to repository helper |
| `CHECK_SECURITY_UPDATES`, `CHECK_DISK_SPACE` | Required `true`/`false` switches |
| `DISK_WARNING`, `DISK_CRITICAL` | Percent thresholds, default 85/95; warning must be lower |
| `DISK_MAX_DEPTH` | Largest-child descent levels, default 3, allowed 1–10 |
| `DISK_DIAG_TIMEOUT` | Total `du` time budget per filesystem, default 60 seconds, allowed 1–600 |
| `CHECKER_STATE_DIR` | Absolute persistent state/lock directory, default `/var/lib/self-hosted-checker` |

Keep SMTP passwords in the notifier/msmtp configuration. `.env` and checker
runtime state are ignored by Git. systemd creates the default state directory
with mode 0700; a custom directory must be protected by the administrator.
Use the same state directory for scheduled and manual runs to share the lock
and notification history.

## Execution and logs

```bash
sudo ./checker/run.sh
sudo ./checker/run.sh disk-space
sudo ./checker/run.sh security-updates
sudo systemctl start checker.service
systemctl list-timers checker.timer
sudo journalctl -u checker.service --since today
```

An explicitly selected check still respects its enable switch. Configuration
is validated before any check or APT refresh. Each enabled check runs even when
another fails; the final status is nonzero if any check or mail delivery fails.
An attention condition with a successful notification is not an execution
failure. Exit 2 indicates invalid CLI usage. Concurrent scheduled/manual runs
fail immediately on a shared `flock`; locks release automatically on exit.
The service has a 45-minute total limit; APT refresh and mail retain their
15-minute and two-minute limits. Very large mount inventories may require
adjusting the service limit. Start/completion/failure messages go to the journal;
healthy checks produce no mail.

Security notifications include sorted package versions and reboot reasons;
identical reports are suppressed. A changed report sends another email. An OK
run clears suppression, so a subsequent recurrence alerts again. Legacy direct
security checker runs retain their existing daily notification behavior.
For an APT preview without refresh or email, use its documented
`--no-refresh --dry-run` CLI.

## Disk policy and diagnostic limits

Mounts come from `/proc/self/mountinfo`; capacity and inodes use `statvfs`, the
same kernel counters used by `df`. Capacity percent uses used plus available
blocks (excluding reserved blocks from available space), rounded up. Inode
percent is rounded up; zero inode totals mean unsupported accounting.
Either capacity or inode usage at a threshold produces WARNING/CRITICAL.

Included types are ext2/3/4, XFS, Btrfs, ZFS, FAT/exFAT, NTFS/NTFS3/fuseblk,
NFS/NFS4, CIFS and SSHFS. Virtual filesystems, tmpfs, overlay, automounts and
unlisted types are excluded. One topmost mount per device is checked to avoid
bind-mount duplicates; distinct subvolume usage/quotas are not independently
measured. Run on the actual host, not in a container mount namespace. Network
mounts can block kernel queries; the service timeout bounds the complete run.

For a new alert, changed severity or an increase of at least one reported
percentage point since the last successful notice, `du -x` scans the affected
mount. It identifies the largest immediate subdirectory by allocated bytes,
then repeats inside it up to the configured depth. It stays on the filesystem;
this scan measures space, not which directories consume inodes. Direct files
can explain usage even when there is no child directory to descend into.
No diagnostic scan or email is repeated for an unchanged alert. Recovery is
recorded silently; the next recurrence sends a new notice. Filesystems no longer
mounted are removed from suppression history.

The `du` budget applies across all descent levels for each alerted filesystem.
Permission errors, changing files, I/O failures or timeout are reported as an
incomplete diagnosis and make the check fail. Mail still includes the observed
occupancy, but failed diagnostics/delivery do not advance suppression state,
allowing a retry. A material difference between filesystem used bytes and the
first `du` total (over 10% and over 1 GiB) is explicitly reported. Deleted-open
files, filesystem metadata, snapshots, subvolumes or contents hidden beneath
other mounts can account for differences. No scan is a consistent snapshot and
these causes are not automatically attributed. No cleanup is performed.

State contains only the last notified percentages/status per filesystem and
the last security report. It is written atomically only after successful mail.
Unreadable/malformed state fails visibly; move the state JSON aside to reset it
and receive fresh attention notices. Losing state may repeat an alert.

## Migration, removal and validation

For an existing standalone security installation, configure this `.env` with the
same recipient/account and disable its old timer deliberately on the host:

```bash
sudo systemctl disable --now security-update-check.timer
sudo ./checker/install.sh
```

Do not leave both timers enabled: their different suppression policies can send
duplicate mail and concurrent APT refreshes may fail. Existing standalone paths,
units and environment variables remain supported for rollback. To revert,
disable the checker timer and restore the old timer deliberately.

To uninstall (does not delete state, configuration, or repository files):

```bash
sudo systemctl disable --now checker.timer
# Wait for an active checker.service to finish before removing units.
sudo rm /etc/systemd/system/checker.service /etc/systemd/system/checker.timer
sudo systemctl daemon-reload
```

Local deterministic tests use temporary files, fake APT/mail and controlled
failures; they do not contact production services or send mail:

```bash
python3 -m unittest discover -s checker/tests -v
./check.sh
```
