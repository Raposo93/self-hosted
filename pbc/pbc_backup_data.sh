#!/usr/bin/env bash

# ========================================== #
# Backup data with Proxmox Backup Client     #
# Logs results and sends email notification  #
# ========================================== #

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

REPO_DIR="$(dirname -- "$SCRIPT_DIR")"
SEND_MAIL="$REPO_DIR/mail-notifier/send-mail.sh"

if [[ ! -x "$SEND_MAIL" ]]; then
    echo "Error: Mail notifier not found or not executable: $SEND_MAIL" >&2
    exit 1
fi

: "${LOGFILE:?Missing LOGFILE}"
: "${SOURCE_DIR:?Missing SOURCE_DIR}"
: "${REPO:?Missing REPO}"
: "${BACKUP_NAME:?Missing BACKUP_NAME}"
: "${RECIPIENT_EMAIL:?Missing RECIPIENT_EMAIL}"
: "${SENDER_EMAIL:?Missing SENDER_EMAIL}"
: "${MSMTP_ACCOUNT:?Missing MSMTP_ACCOUNT}"

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "Error: Source directory does not exist: $SOURCE_DIR" >&2
    exit 1
fi

mkdir -p "$(dirname -- "$LOGFILE")"
: > "$LOGFILE"

SECONDS=0
START_TIME="$(date +"%Y-%m-%d %H:%M:%S")"
HOSTNAME="$(hostname -f 2>/dev/null || hostname)"

log() {
    echo "$*" >> "$LOGFILE"
}

log "========== Backup started at $START_TIME =========="
log "Host: $HOSTNAME"
log "Source: $SOURCE_DIR"
log "Archive: $BACKUP_NAME"

ENCRYPTION_ARGS=()

if [[ -n "${ENCRYPTION_KEYFILE:-}" ]]; then
    if [[ ! -r "$ENCRYPTION_KEYFILE" ]]; then
        echo "Error: Encryption key file not found or not readable: $ENCRYPTION_KEYFILE" >&2
        exit 1
    fi

    ENCRYPTION_ARGS=(--keyfile "$ENCRYPTION_KEYFILE")
fi

set +e

proxmox-backup-client backup "$BACKUP_NAME:$SOURCE_DIR" \
    --repository "$REPO" \
    "${ENCRYPTION_ARGS[@]}" \
    --change-detection-mode metadata \
    --skip-e2big-xattr \
    >> "$LOGFILE" 2>&1

STATUS=$?

set -e

END_TIME="$(date +"%Y-%m-%d %H:%M:%S")"
DURATION="$SECONDS"

log "Backup exit code: $STATUS"
log "Duration: ${DURATION}s"
log "========== Backup ended at $END_TIME =========="

if [[ "$STATUS" -ne 0 ]]; then
    SUBJECT="❌ Backup failed: $BACKUP_NAME on $HOSTNAME"
else
    SUBJECT="✅ Backup completed: $BACKUP_NAME on $HOSTNAME"
fi

if ! "$SEND_MAIL" \
    --account "$MSMTP_ACCOUNT" \
    --from "$SENDER_EMAIL" \
    --to "$RECIPIENT_EMAIL" \
    --subject "$SUBJECT" \
    < "$LOGFILE"
then
    log "Warning: Failed to send notification email"
fi

exit "$STATUS"
