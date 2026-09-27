# Security update check

Daily host-local monitoring for Ubuntu, Debian, Proxmox VE and Proxmox Backup
Server. This utility refreshes APT metadata and reports pending security updates
and reboot requirements through [mail-notifier](../mail-notifier/README.md).
It never installs packages or reboots a host. No unattended upgrades are enabled.

## Detection policy

The checker uses `python3-apt` and signed APT Release metadata. Installed packages
are reported when an available newer version has a trusted origin of `Debian`
with a suite ending in `-security`, or `Ubuntu`, `UbuntuESM`, `UbuntuESMApps` with
a suite ending in `-security`. It examines all available versions, so a newer
non-security candidate does not hide an outstanding security fix. Each line
includes the installed version, security version and current APT candidate;
held packages remain visible. It does not resolve a transaction or bypass pins
or holds. The administrator must review whether an update is installable.

On hosts with `proxmox-ve`, `pve-manager` or `proxmox-backup-server` installed,
the report also includes every newer trusted version from an origin of
`Proxmox`, and packages starting with `pve-`, `proxmox-`, `libpve-` or
`libproxmox-`. This covers vendor packages and kernel metapackages which pull
in newly named kernels. These entries are labelled **Proxmox review**: they
are conservatively treated as requiring attention, without claiming a known
CVE. Debian security updates are checked on these hosts too.

Reboot checks run independently of pending updates:

* `/run/reboot-required` and, when present, its `.pkgs` companion;
* installed `/boot/vmlinuz-*` releases newer than `uname -r`, using Debian's
  version comparison;
* a running kernel image modified after the boot time from `/proc/stat`.

The last two checks cover kernel changes when Debian/Proxmox does not create a
reboot marker. Reboot reasons can result from ordinary maintenance too; the
checker cannot reliably attribute them to a particular security advisory.

States are `OK`, `SECURITY UPDATES`, `REBOOT REQUIRED`, or both attention states.
Normal successful `OK` runs print nothing and send no email. While attention
remains necessary, each daily run sends one email containing the hostname,
package versions and reboot reasons. No state file or suppression cache is used.

## Installation and configuration

Supported baseline: Debian 11+, Ubuntu 22.04+, or Proxmox based on these releases,
with APT supporting `APT::Update::Error-Mode=any`. Run on the actual host, with
its security and applicable Proxmox repositories configured and accessible.
ESM coverage requires the host's enabled entitlement repositories.

Install dependencies and configure the shared notifier for root as described in
its README:

```bash
sudo apt-get install python3 python3-apt msmtp ca-certificates
chmod +x mail-notifier/send-mail.sh
cp security-update-check/.env.example security-update-check/.env
chmod 600 security-update-check/.env
```

Edit `.env`:

| Variable | Meaning |
| --- | --- |
| `RECIPIENT_EMAIL` | Required recipient |
| `MSMTP_ACCOUNT` | Optional msmtp account; empty uses its default |
| `SENDER_EMAIL` | Optional From header; empty uses the notifier/msmtp configuration |

Keep SMTP credentials in the shared notifier configuration, never in this file
or Git. Use `/usr/bin/python3`, since a virtual environment or independently
installed Python may not have the distribution's `python3-apt` bindings.

## systemd deployment

Adapt both `/path/to/self-hosted` paths in the service to the actual checkout.
The service runs as root to refresh APT lists and use the root notifier setup.
The daily calendar timer has up to one hour of random delay and catches a missed
run after downtime. systemd serializes starts of this single service; avoid
running the manual checker alongside it. Concurrent APT operations can cause a
visible failure and should be retried after the package manager finishes.

The following are installation commands to run deliberately on each host:

```bash
sudo install -m 644 security-update-check/security-update-check.service.example \
  /etc/systemd/system/security-update-check.service
sudo install -m 644 security-update-check/security-update-check.timer.example \
  /etc/systemd/system/security-update-check.timer
sudoedit /etc/systemd/system/security-update-check.service
sudo systemd-analyze verify /etc/systemd/system/security-update-check.{service,timer}
sudo systemctl daemon-reload
sudo systemctl enable --now security-update-check.timer
systemctl list-timers security-update-check.timer
```

Only the timer is enabled; the service is a static oneshot. To stop monitoring,
disable it with `sudo systemctl disable --now security-update-check.timer`.
Removing the units and reloading systemd does not affect packages or host data.

## Manual validation

Preview using existing APT lists, without mail or package-manager writes:

```bash
/usr/bin/python3 security-update-check/security_update_check.py --no-refresh --dry-run
```

This always prints the report, including `OK`. It may be stale. For an accurate
preview, refresh metadata (requires root) without sending mail:

```bash
sudo /usr/bin/python3 security-update-check/security_update_check.py --dry-run
```

This runs only `apt-get update`, including any locally configured APT update
hooks. No upgrade command is run. Partial repository refresh failures cause a
non-zero result rather than reporting stale data as current.

Run with the configured environment and send mail if attention is needed:

```bash
sudo systemctl start security-update-check.service
sudo journalctl -u security-update-check.service --since today
```

Use the notifier's own documented test procedure to verify mail transport even
when this checker reports `OK`. Deterministic tests exercise security origins,
held/superseded updates, Proxmox/kernel policy, reboot markers, quiet runs,
refresh failure and mail invocation without refreshing APT or sending mail:

```bash
python3 -m unittest discover -s security-update-check/tests -v
./check.sh
```

## Failures and limitations

Exit `0` means the check completed and any needed notification was sent (or was
previewed with `--dry-run`); pending updates themselves do not fail the service.
Exit `1` means checking or sending failed. Dependency, APT, filesystem and mail
errors appear in the journal; no success is reported after a failed refresh.
APT refresh has a 15-minute timeout, mail has a two-minute timeout and systemd
limits the whole run to 20 minutes. Failures do not send an additional email
through a possibly broken transport; inspect failed units/journal separately.

* Missing/disabled repositories, expired subscriptions, unsupported releases
  and unavailable ESM metadata can hide security fixes. An `OK` result is not
  proof that the host is supported or free of vulnerabilities.
* This is a package-origin check, not advisory/severity analysis. It ignores
  third-party security repositories, containers, snaps, manually installed
  software and security fixes shipped only through ordinary update suites.
* Proxmox vendor updates include non-security changes, intentionally producing
  false positives. Packages provided by a differently labelled mirror may need
  policy changes; official metadata is the baseline.
* A newer installed but deliberately pinned/fallback kernel can trigger repeated
  reboot alerts. The check does not determine the next bootloader selection.
  Custom image layouts and preserved timestamps can miss kernel replacements.
* Userspace restart needs and microcode/firmware changes without a reboot marker
  are not comprehensively detected. Kernel live-patching can make a reboot alert
  conservative. This utility does not replace update instructions or restore
  planning for a hypervisor.

References: [python-apt package and origin API](https://apt-team.pages.debian.net/python-apt/library/apt.package.html),
[Proxmox update policy](https://github.com/proxmox/pve-docs/blob/master/system-software-updates.adoc),
[Proxmox firmware and microcode guidance](https://github.com/proxmox/pve-docs/blob/master/firmware-updates.adoc).
