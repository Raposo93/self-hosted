#!/usr/bin/env bash

set -u

if (( $# != 2 )); then
    echo "Usage: $0 <name> <url>" >&2
    exit 2
fi

NAME="$1"
URL="$2"

if [[ ! "$NAME" =~ ^[a-zA-Z0-9._-]+$ ]]; then
    echo "Error: Invalid monitor name: $NAME." >&2
    echo "Allowed characters: a-z, A-Z, 0-9, dot, underscore and hyphen." >&2
    exit 2
fi

if [[ ! "$URL" =~ ^https?:// ]]; then
    echo "Error: URL must start with http:// or https://: $URL" >&2
    exit 2
fi

FAIL_LIMIT=3
CONNECT_TIMEOUT=5
MAX_TIME=15
ALERT_EMAIL="g.raposo@horizontes-informatica.com"

SCRIPT_DIR="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &&
    pwd
)"

LOG_FILE="${SCRIPT_DIR}/http-endpoint-monitor.log"
STATE_DIR="${SCRIPT_DIR}/state"
STATE_FILE="${STATE_DIR}/${NAME}.state"
LOCK_FILE="${STATE_DIR}/${NAME}.lock"

mkdir -p "$STATE_DIR"

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

log_message() {
    printf '%s [%s] %s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" \
        "$NAME" \
        "$1" >> "$LOG_FILE"
}

send_alert() {
    local message="$1"
    local subject="HTTP monitor: ${NAME}"
    local from="g.raposo@horizontesinformatica.com"

    logger -p user.warning -t http-monitor "$message"

    if [[ -n "$ALERT_EMAIL" ]] && command -v msmtp >/dev/null 2>&1; then
        {
            printf 'From: HTTP Monitor <%s>\n' "$from"
            printf 'To: %s\n' "$ALERT_EMAIL"
            printf 'Subject: %s\n' "$subject"
            printf 'Content-Type: text/plain; charset=UTF-8\n'
            printf 'Content-Transfer-Encoding: 8bit\n'
            printf '\n'
            printf '%s\n' "$message"
        } | msmtp "$ALERT_EMAIL"
    fi
}

previous_state="UNKNOWN"
failure_count=0

if [[ -r "$STATE_FILE" ]]; then
    IFS='|' read -r previous_state failure_count < "$STATE_FILE"
fi

[[ "$failure_count" =~ ^[0-9]+$ ]] || failure_count=0

error_file="$(mktemp)"

result="$(
    curl \
        --location \
        --silent \
        --show-error \
        --output /dev/null \
        --write-out '%{http_code}|%{time_total}' \
        --connect-timeout "$CONNECT_TIMEOUT" \
        --max-time "$MAX_TIME" \
        "$URL" 2>"$error_file"
)"
curl_result=$?

curl_error="$(tr '\n' ' ' < "$error_file")"
rm -f "$error_file"

http_code="${result%%|*}"
response_time="${result#*|}"

[[ "$http_code" =~ ^[0-9]{3}$ ]] || http_code="000"

if [[ "$curl_result" -eq 0 && "$http_code" =~ ^[23] ]]; then
    log_message "OK http=${http_code} tiempo=${response_time}s"

    if [[ "$previous_state" == "DOWN" ]]; then
        send_alert "RECUPERADO: ${NAME} responde de nuevo. HTTP ${http_code}, ${response_time}s."
    fi

    printf 'UP|0\n' > "$STATE_FILE"
    exit 0
fi

failure_count=$((failure_count + 1))

details=""

if [[ -n "$curl_error" ]]; then
    details=" error_curl=${curl_error}"
fi

log_message "FALLO contador=${failure_count}/${FAIL_LIMIT} http=${http_code} curl=${curl_result} tiempo=${response_time}s${details}"

if (( failure_count >= FAIL_LIMIT )); then
    if [[ "$previous_state" != "DOWN" ]]; then
        send_alert "CAÍDA: ${NAME} lleva ${failure_count} fallos. HTTP ${http_code}, curl=${curl_result}. ${curl_error}"
    fi

    printf 'DOWN|%s\n' "$failure_count" > "$STATE_FILE"
else
    printf '%s|%s\n' "$previous_state" "$failure_count" > "$STATE_FILE"
fi
