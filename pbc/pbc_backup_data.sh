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

BACKUP_ID_ARGS=()
if [[ ${BACKUP_ID+x} ]]; then
    if [[ ! "$BACKUP_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
        echo "Error: BACKUP_ID must start with an alphanumeric character and contain only letters, digits, _, . or -" >&2
        exit 1
    fi
    BACKUP_ID_ARGS=(--backup-id "$BACKUP_ID")
fi

NAMESPACE_ARGS=()
if [[ -n "${NAMESPACE:-}" ]]; then
    NAMESPACE_ARGS=(--ns "$NAMESPACE")
fi

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "Error: Source directory does not exist: $SOURCE_DIR" >&2
    exit 1
fi

SOURCE_REAL="$(realpath -e -- "$SOURCE_DIR")"
if [[ ${EXPECTED_MOUNT+x} ]]; then
    if [[ -z "$EXPECTED_MOUNT" || ! -d "$EXPECTED_MOUNT" ]]; then
        echo "Error: EXPECTED_MOUNT must name an existing directory" >&2
        exit 1
    fi
    MOUNT_REAL="$(realpath -e -- "$EXPECTED_MOUNT")"
    if [[ "$SOURCE_REAL" != "$MOUNT_REAL" && "$SOURCE_REAL" != "${MOUNT_REAL%/}/"* ]]; then
        echo "Error: SOURCE_DIR is outside EXPECTED_MOUNT: $EXPECTED_MOUNT" >&2
        exit 1
    fi
    if ! ACTUAL_MOUNT="$(findmnt --target "$SOURCE_REAL" --noheadings --output TARGET)"; then
        echo "Error: Cannot determine the mount containing SOURCE_DIR" >&2
        exit 1
    fi
    if [[ "$ACTUAL_MOUNT" != "$MOUNT_REAL" ]]; then
        echo "Error: Expected mount $MOUNT_REAL is absent from SOURCE_DIR (found $ACTUAL_MOUNT)" >&2
        exit 1
    fi
    if [[ ${EXPECTED_MOUNT_SOURCE+x} ]]; then
        if [[ -z "$EXPECTED_MOUNT_SOURCE" ]]; then
            echo "Error: EXPECTED_MOUNT_SOURCE must not be empty" >&2
            exit 1
        fi
        if ! ACTUAL_SOURCE="$(findmnt --target "$SOURCE_REAL" --noheadings --output SOURCE)"; then
            echo "Error: Cannot determine the mount source for SOURCE_DIR" >&2
            exit 1
        fi
        if [[ "$ACTUAL_SOURCE" != "$EXPECTED_MOUNT_SOURCE" ]]; then
            echo "Error: Unexpected mount source for $MOUNT_REAL: $ACTUAL_SOURCE" >&2
            exit 1
        fi
    fi
elif [[ ${EXPECTED_MOUNT_SOURCE+x} ]]; then
    echo "Error: EXPECTED_MOUNT_SOURCE requires EXPECTED_MOUNT" >&2
    exit 1
fi

INCLUDE_DEV_ARGS=()
if [[ -n "${INCLUDE_DEV_MOUNTS:-}" ]]; then
    if [[ "$INCLUDE_DEV_MOUNTS" == '|'* || "$INCLUDE_DEV_MOUNTS" == *'|' || "$INCLUDE_DEV_MOUNTS" == *'||'* ]]; then
        echo "Error: INCLUDE_DEV_MOUNTS contains an empty entry" >&2
        exit 1
    fi
    IFS='|' read -r -a INCLUDE_MOUNTS <<< "$INCLUDE_DEV_MOUNTS"
    for included in "${INCLUDE_MOUNTS[@]}"; do
        if [[ "$included" != /* || ! -d "$included" ]]; then
            echo "Error: INCLUDE_DEV_MOUNTS entries must be existing absolute directories: $included" >&2
            exit 1
        fi
        included_real="$(realpath -e -- "$included")"
        if [[ "$included_real" != "$SOURCE_REAL/"* ]]; then
            echo "Error: INCLUDE_DEV_MOUNTS entry is outside SOURCE_DIR: $included" >&2
            exit 1
        fi
        if ! included_mount="$(findmnt --mountpoint "$included_real" --noheadings --output TARGET)" || [[ "$included_mount" != "$included_real" ]]; then
            echo "Error: INCLUDE_DEV_MOUNTS entry is not mounted: $included" >&2
            exit 1
        fi
        INCLUDE_DEV_ARGS+=(--include-dev "$included_real")
    done
fi

mkdir -p "$(dirname -- "$LOGFILE")"
: > "$LOGFILE"

SECONDS=0
START_TIME="$(date +"%Y-%m-%d %H:%M:%S")"
HOSTNAME="$(hostname -f 2>/dev/null || hostname)"

log() {
    echo "$*" >> "$LOGFILE"
}

report_health() {
    local phase="$1" endpoint="${HEALTHCHECK_URL%/}" curl_status
    case "$phase" in
        start) endpoint+="/start" ;;
        fail) endpoint+="/fail" ;;
    esac
    if curl --fail --silent --show-error --output /dev/null \
        --connect-timeout 5 --max-time 10 "$endpoint" 2>/dev/null; then
        log "Healthcheck $phase reported"
    else
        curl_status=$?
        log "Warning: Healthcheck $phase failed (curl exit code: $curl_status)"
    fi
}

log "========== Backup started at $START_TIME =========="
log "Host: $HOSTNAME"
log "Source: $SOURCE_DIR"
log "Archive: $BACKUP_NAME"
if [[ ${BACKUP_ID+x} ]]; then
    log "Backup ID: $BACKUP_ID"
fi

ENCRYPTION_KEYFILE_SET=false
ENCRYPTION_CREDENTIAL_SET=false

[[ -n "${ENCRYPTION_KEYFILE:-}" ]] && ENCRYPTION_KEYFILE_SET=true
[[ -f "${CREDENTIALS_DIRECTORY:-}/proxmox-backup-client.encryption-password" ]] \
    && ENCRYPTION_CREDENTIAL_SET=true

if [[ "$ENCRYPTION_KEYFILE_SET" != "$ENCRYPTION_CREDENTIAL_SET" ]]; then
    echo "Error: Incomplete encryption configuration" >&2
    exit 1
fi

ENCRYPTION_ARGS=()

if [[ "$ENCRYPTION_KEYFILE_SET" == true ]]; then
    if [[ ! -r "$ENCRYPTION_KEYFILE" ]]; then
        echo "Error: Encryption key file not found or not readable: $ENCRYPTION_KEYFILE" >&2
        exit 1
    fi

    ENCRYPTION_ARGS=(--keyfile "$ENCRYPTION_KEYFILE")
fi

PRE_EXIT=0
POST_EXIT=0
PBC_EXIT=0
BACKUP_ATTEMPTED=false

if [[ -n "${PRE_BACKUP_HOOK:-}" && ( ! -f "$PRE_BACKUP_HOOK" || ! -x "$PRE_BACKUP_HOOK" ) ]]; then
    log "Error: Pre-backup hook is not an executable file: $PRE_BACKUP_HOOK"
    PRE_EXIT=126
elif [[ -n "${POST_BACKUP_HOOK:-}" && ( ! -f "$POST_BACKUP_HOOK" || ! -x "$POST_BACKUP_HOOK" ) ]]; then
    log "Error: Post-backup hook is not an executable file: $POST_BACKUP_HOOK"
    POST_EXIT=126
else
    if [[ -n "${PRE_BACKUP_HOOK:-}" ]]; then
        log "Running pre-backup hook: $PRE_BACKUP_HOOK"
        if "$PRE_BACKUP_HOOK" >> "$LOGFILE" 2>&1; then
            log "Pre-backup hook exit code: 0"
        else
            PRE_EXIT=$?
            log "Pre-backup hook exit code: $PRE_EXIT"
        fi
    fi

    if [[ "$PRE_EXIT" -eq 0 ]]; then
        if [[ -n "${HEALTHCHECK_URL:-}" ]]; then
            report_health start
        fi

        BACKUP_ATTEMPTED=true
        if proxmox-backup-client backup "$BACKUP_NAME:$SOURCE_DIR" \
            --repository "$REPO" \
            "${BACKUP_ID_ARGS[@]}" \
            "${NAMESPACE_ARGS[@]}" \
            "${ENCRYPTION_ARGS[@]}" \
            "${INCLUDE_DEV_ARGS[@]}" \
            --change-detection-mode metadata \
            --skip-e2big-xattr \
            >> "$LOGFILE" 2>&1; then
            PBC_EXIT=0
        else
            PBC_EXIT=$?
        fi

        if [[ -n "${HEALTHCHECK_URL:-}" ]]; then
            if [[ "$PBC_EXIT" -eq 0 ]]; then
                report_health success
            else
                report_health fail
            fi
        fi

        if [[ -n "${POST_BACKUP_HOOK:-}" ]]; then
            log "Running post-backup hook: $POST_BACKUP_HOOK"
            if "$POST_BACKUP_HOOK" >> "$LOGFILE" 2>&1; then
                log "Post-backup hook exit code: 0"
            else
                POST_EXIT=$?
                log "Post-backup hook exit code: $POST_EXIT"
            fi
        fi
    fi
fi

END_TIME="$(date +"%Y-%m-%d %H:%M:%S")"
DURATION="$SECONDS"

if [[ "$BACKUP_ATTEMPTED" == true ]]; then
    log "Backup exit code: $PBC_EXIT"
else
    log "Backup not started"
fi
STATUS=0
if [[ "$PRE_EXIT" -ne 0 ]]; then
    STATUS="$PRE_EXIT"
elif [[ "$PBC_EXIT" -ne 0 ]]; then
    STATUS="$PBC_EXIT"
elif [[ "$POST_EXIT" -ne 0 ]]; then
    STATUS="$POST_EXIT"
fi
log "Overall exit code: $STATUS"
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
