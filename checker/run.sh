#!/usr/bin/env bash
set -Eeuo pipefail
umask 0077
DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ $# -le 1 ]] || { echo 'Usage: run.sh [disk-space|security-updates]' >&2; exit 2; }
selection="${1:-all}"
case "$selection" in all|disk-space|security-updates) ;; *) echo 'Unknown check' >&2; exit 2 ;; esac
[[ -f "$DIRECTORY/.env" ]] || { echo 'Missing checker/.env' >&2; exit 1; }
set -a
# Configuration is a trusted, root-owned Bash environment file.
# shellcheck disable=SC1091
source "$DIRECTORY/.env"
set +a
export CHECKER_STATE_DIR="${CHECKER_STATE_DIR:-/var/lib/self-hosted-checker}"
export SEND_MAIL="${SEND_MAIL:-$DIRECTORY/../mail-notifier/send-mail.sh}"
export HOST_NAME="${HOST_NAME:-$(hostname)}"
/usr/bin/python3 "$DIRECTORY/checks/config.py"
mkdir -p -- "$CHECKER_STATE_DIR"
exec 9>"$CHECKER_STATE_DIR/run.lock"
flock -n 9 || { echo 'Another checker run is active' >&2; exit 1; }
result=0
for check in security-updates disk-space; do
    if [[ "$selection" != all && "$selection" != "$check" ]]; then continue; fi
    if [[ "$check" == security-updates && "$CHECK_SECURITY_UPDATES" == false ]]; then continue; fi
    if [[ "$check" == disk-space && "$CHECK_DISK_SPACE" == false ]]; then continue; fi
    echo "Starting $check"
    if /usr/bin/python3 "$DIRECTORY/checks/$check.py"; then
        echo "Completed $check"
    else
        echo "Failed $check" >&2
        result=1
    fi
done
exit "$result"
