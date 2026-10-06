# Proxmox restore check

Restores the latest PBS snapshot of each configured VM (`vm`) or container (`ct`)
sequentially on a **dedicated standalone Proxmox VE test node**, after checking
the configured maximum snapshot age. It checks running state and optionally QEMU Guest Agent ping for VMs, or `/bin/true` through
`pct exec` for containers. It stops and destroys each temporary guest, sends one
plain-text summary through `../mail-notifier/send-mail.sh`, then optionally
powers off. This measures a real restore and basic boot, not application health,
data integrity, or the ability to recover production hardware/networking.
A VM checked only for running state is a warning: QEMU running does not prove
that its operating system booted.

## Requirements and safety

Use root, Python 3.10+, PVE CLI (`pvesh`, `pvesm`, `qmrestore`, `qm`, `pct`),
systemd, and the configured shared mail notifier/msmtp. Configure PBS through
PVE storage administration first, including TLS fingerprint/CA, credentials,
namespace and encryption keys when applicable; keep these outside Git.
The script uses the registered PBS storage, never receives PBS passwords.
Use credentials with read/restore access only where practical.

Do not install this service on production. Hostname and absence of
`/etc/pve/corosync.conf` are mandatory gates, not proof a host is expendable.
The temporary ID must be unused by both VM and LXC and different from all source
IDs. An existing guest is never overwritten or adopted for cleanup. A local
nonblocking lock prevents concurrent runs of this tool; reserve the temporary
ID exclusively and do not create/edit guests manually during a run. The target
must be local `dir`, `lvmthin`, or `zfspool`, dedicated to tests, with enough
space for the largest restored guest. No shared production disks or mounts.
Check actual storage paths: the PVE storage type alone cannot establish physical
separation. The node must have sufficient RAM for each guest in turn. VM CPU
topology is capped to the host's available logical CPUs before boot.

Restores do not start guests automatically. Before explicit startup, the script
rewrites the restored configuration using an allowlist of boot essentials:
**all NICs are removed**; passthrough, custom QEMU arguments, hook scripts,
serial devices, LXC raw configuration/features, host devices, bind mounts,
startup settings and unknown options are removed. ISO/CD-ROM drives are removed.
Retained disks/mount volumes must point to the configured target storage and
belong to the reserved temporary ID or
startup is refused. This deliberately changes the test hardware and may stop
some guests from booting. Network-dependent boot cannot be validated offline.
For VMs, the script keeps `cores`, `sockets` and `vcpus` when the maximum
`cores * sockets` fits. Otherwise it changes the temporary VM to one socket
and at most the allowed number of cores, and caps `vcpus` if present. The limit
is the smaller of the host's available logical CPU count and optional
`max_test_vcpus`. The PBS backup and source VM are never edited.
LXC backups with host bind/device mounts are refused before restore; create
a backup without these mounts for this drill. No isolated bridge or routing
configuration is needed. Treat backups as trusted
input; this is not a sandbox for hostile guest kernels/restore archives.

## Configuration and installation

Copy `config.example.json` to `/etc/proxmox-restore-check.json` on the test node,
owned by root with mode 600. Adapt `hostname` to `hostname` output, PBS and target
storage IDs, reserved `temporary_id`, recipient and explicit guest inventory.
JSON avoids an additional YAML dependency. Keep the private config outside Git.
`agent: true` requires a configured, working QEMU Guest Agent in that backup.
`boot_timeout` covers start plus state/health polling. `restore_timeout` bounds
each restore command. `max_test_vcpus` is an optional positive integer for a
stricter DR drill CPU cap; omit it to use host capacity. CLI calls and cleanup
also have bounded timeouts.

Configure [mail-notifier](../mail-notifier/README.md) for root, ensuring
`send-mail.sh` is executable. First run manually on the dedicated test node:

```bash
sudo python3 /path/to/self-hosted/proxmox-restore-check/proxmox_restore_check.py \
  --config /etc/proxmox-restore-check.json --no-poweroff
```

Review the report, PVE configurations and absence of leftover volumes. Perform
an actual VM and LXC drill with your storage/backend before enabling unattended
runs; local fixture tests do not establish compatibility with a deployed PVE
version or prove any production backup is recoverable.

Adapt paths in the service example, install it as
`/etc/systemd/system/proxmox-restore-check.service`, then on the **test node**:

```bash
sudo systemctl daemon-reload
sudo systemctl enable proxmox-restore-check.service
```

Do not use `enable --now` unless you intend to run a drill immediately. The unit
runs on each boot, including manual boots. After validating the manual drill,
set `poweroff: true` for unattended operation; `--no-poweroff` always overrides
it. The example defaults to false to allow installation/debugging. An external
always-on host should send Wake-on-LAN monthly using its own systemd timer;
this component owns neither WoL nor a timer. Verify firmware/NIC WoL and ability
to wake after poweroff independently.

## Snapshot age policy

`max_snapshot_age_seconds` sets an optional global maximum age in seconds.
A guest's `max_snapshot_age_seconds` overrides it; omitted or `null` inherits
the global value. Positive integers enable the limit, and `0` explicitly
disables it, including for one guest when the global limit is enabled.
The global default is `0`, so existing configurations retain unlimited age.
The example enables seven days globally and two days for its VM; adapt these
values to the backup schedule and recovery requirements of each machine.
Negative values, booleans, fractions, and strings are rejected.

Dates are parsed as real UTC calendar timestamps from matching PBS volume IDs.
Malformed matching dates fail selection instead of silently choosing an older
backup. The selected backup is compared with the current UTC time immediately
before restoration. An age exactly equal to the limit is accepted; a greater
age fails before restore, startup, or temporary guest creation. No older copy
is substituted. The report records the selected volume (including its UTC
date), age in seconds, and applied limit, and the guest contributes FAIL to
the summary and non-zero exit status. Other guests can still be tested.

`future_tolerance_seconds` is a global non-negative integer, default `300`.
Dates up to that many seconds ahead are accepted for small clock differences;
a date further ahead fails even if the age limit is disabled. The report uses
a negative age for dates in the future. Keep the test host clock synchronized.

An acceptable age only establishes freshness under this policy. Successful
restoration and the basic boot check remain separate results; neither proves
application health or data integrity.

## Results, failures and recovery

The journal/stdout and single email include source ID/name, selected backup
timestamp, age and applied limit, restore/boot duration on success, health check
result, removed options, CPU reductions, failure reason and cleanup outcome. A CPU reduction alone is
not a failure. Totals distinguish OK, WARN, FAIL and SKIP.
Each latest backup is chosen by UTC timestamp from PVE's JSON PBS volume list.
The configured age policy is enforced; application checks are not performed.

One guest failure continues to the next only after safe cleanup. CLI timeouts
kill the CLI process group before cleanup. Failed cleanup stops further restores
and marks remaining guests skipped. A failed/locked partial restore may require
manual disk/configuration cleanup; inspect PVE task logs and target volumes.
No forced unlock, forced overwrite or deletion of unrelated disks is attempted.
A restore failure before configuration creation can leave orphan volumes: inspect
storage after such failures. Abrupt power loss, SIGKILL or host failure cannot
guarantee cleanup; the next run refuses an occupied temporary ID.

On SIGTERM or Ctrl-C, the current CLI process group is stopped and waited for
before cleanup. The same temporary-ID and disk-reference checks still apply.
The current guest is reported as failed, remaining guests are skipped, and the
script attempts one failure-summary email. It returns non-zero and does not
power off automatically after cancellation, leaving the node available for
inspection. A failed cleanup is reported as requiring manual intervention.
The example unit uses `KillMode=mixed` so systemd signals the Python process
first and gives it time to stop CLI workers and clean up. `TimeoutStopSec=10min`
bounds that work before systemd force-kills the service; adapt the limit if
your storage needs longer. These steps cannot guarantee cleanup after SIGKILL,
power loss, a stuck PVE server-side task, or a cleanup error. Inspect PVE task
logs and target storage before retrying if cancellation interrupts a restore.

Exit 0 means no failed/skipped guest and successful mail submission (warnings
are allowed); exit 1 also covers mail or poweroff failure. SMTP failure never
turns a failed drill into success. The summary is printed before mail, and mail
is attempted before poweroff even if a guest failed. A mail failure still permits
configured poweroff; inspect the journal on the next boot. Invalid config,
wrong host, clustered host, occupied ID or other preflight failure abort before
restoration and do not power off. Review refusals with:

```bash
journalctl -u proxmox-restore-check.service
```

To roll back, disable the service on the test node and remove its installed unit;
retain journal/task logs for diagnosis. This repository does not deploy or
operate the node automatically.

## References and local validation

The [Proxmox CLI manuals](https://pve.proxmox.com/pve-docs/) define PBS storage
volume IDs, `qmrestore`, `pct restore`, guest state/agent commands and cleanup.
[Automated DR Test Engine](https://github.com/itpl-pl/Proxmox-VE-Automated-DR-Test-Engine)
provided prior-art ideas (temporary ID, normalization, sequential restore and
cleanup); no source code/dependencies were imported. This implementation omits
its screenshots, service scans and direct SMTP integration.

Run `python3 -m unittest discover -s proxmox-restore-check/tests -v` or the root
`./check.sh`. Fixtures fake commands and configuration files; they never contact
PBS, start guests, send email or power off a host.
