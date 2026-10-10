#!/usr/bin/env bash
set -Eeuo pipefail
DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ $EUID -eq 0 ]] || { echo 'Run install.sh as root on the target host' >&2; exit 1; }
[[ $# -eq 0 ]] || { echo 'Usage: install.sh' >&2; exit 2; }
# systemd paths are deliberately restricted to avoid quoting/substitution errors.
[[ "$DIRECTORY" =~ ^/[a-zA-Z0-9_./-]+$ ]] || { echo 'Checkout path must contain only letters, digits, _, ., / or -' >&2; exit 1; }
[[ -f "$DIRECTORY/.env" ]] || { echo 'Copy .env.example to .env and configure it first' >&2; exit 1; }
for dependency in python3 flock du systemctl systemd-analyze; do
    command -v "$dependency" >/dev/null || { echo "Missing $dependency" >&2; exit 1; }
done
# Validate configuration without refreshing APT or running a check.
set -a
# shellcheck disable=SC1091
source "$DIRECTORY/.env"
set +a
export CHECKER_STATE_DIR="${CHECKER_STATE_DIR:-/var/lib/self-hosted-checker}"
export SEND_MAIL="${SEND_MAIL:-$DIRECTORY/../mail-notifier/send-mail.sh}"
export HOST_NAME="${HOST_NAME:-$(hostname)}"
/usr/bin/python3 "$DIRECTORY/checks/config.py"
if [[ "$CHECK_SECURITY_UPDATES" == true ]]; then
    /usr/bin/python3 -c 'import apt, apt_pkg'
    command -v apt-get >/dev/null
fi
TEMPORARY="$(mktemp -d)"
trap 'rm -rf -- "$TEMPORARY"' EXIT
sed "s|/path/to/self-hosted/checker|$DIRECTORY|g" "$DIRECTORY/checker.service.example" > "$TEMPORARY/checker.service"
cp -- "$DIRECTORY/checker.timer.example" "$TEMPORARY/checker.timer"
systemd-analyze verify "$TEMPORARY/checker.service" "$TEMPORARY/checker.timer"
install -m 644 "$TEMPORARY/checker.service" /etc/systemd/system/checker.service
install -m 644 "$TEMPORARY/checker.timer" /etc/systemd/system/checker.timer
systemctl daemon-reload
systemctl enable --now checker.timer
echo 'checker.timer installed; inspect systemctl list-timers checker.timer'
