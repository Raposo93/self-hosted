#!/usr/bin/env bash

set -Eeuo pipefail

LOG_TAG="shutdown-delayed"

log() {
    logger -t "$LOG_TAG" -- "$*"
}

if [[ "$EUID" -ne 0 ]]; then
    echo "Error: This script must be run as root." >&2
    exit 1
fi

MINUTES="${1:-}"
USER_NAME="${2:-}"
WAYLAND_DISPLAY="${3:-wayland-0}"

if [[ -z "$MINUTES" || -z "$USER_NAME" ]]; then
    echo "Usage: $0 <minutes> <user_name> [wayland_display]" >&2
    exit 1
fi

YAD_PATH="$(command -v yad || true)"

if [[ -z "$YAD_PATH" ]]; then
    echo "Error: 'yad' is not installed. Please install it to use this script." >&2
    exit 1
fi

if ! id "$USER_NAME" &> /dev/null; then
    echo "Error: User '$USER_NAME' does not exist." >&2
    exit 1
fi

if [[ ! "$MINUTES" =~ ^[0-9]+$ ]]; then
    echo "Error: Please enter a valid integer." >&2
    exit 1
fi

if [[ "$MINUTES" -le 0 ]]; then
    echo "Error: Minutes must be a positive integer." >&2
    exit 1
fi

USER_ID="$(id -u "$USER_NAME")"
RUNTIME_DIR="/run/user/$USER_ID"
WAYLAND_SOCKET="$RUNTIME_DIR/$WAYLAND_DISPLAY"
DBUS_SOCKET="$RUNTIME_DIR/bus"

if [[ ! -d "$RUNTIME_DIR" ]]; then
    echo "Error: No active runtime directory found for user '$USER_NAME'." >&2
    exit 1
fi

if [[ ! -S "$WAYLAND_SOCKET" ]]; then
    echo "Error: Wayland socket not found: $WAYLAND_SOCKET" >&2
    exit 1
fi

if [[ ! -S "$DBUS_SOCKET" ]]; then
    echo "Error: D-Bus socket not found: $DBUS_SOCKET" >&2
    exit 1
fi

log "Starting shutdown warning: user=$USER_NAME delay=${MINUTES}m display=$WAYLAND_DISPLAY"

runuser -u "$USER_NAME" -- env \
  XDG_RUNTIME_DIR="$RUNTIME_DIR" \
  WAYLAND_DISPLAY="$WAYLAND_DISPLAY" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=$DBUS_SOCKET" \
  "$YAD_PATH" \
    --title="Scheduled shutdown" \
    --window-icon="system-shutdown" \
    --image="system-shutdown" \
    --text="<span size='large'>The system will shut down in</span>
  <span size='xx-large' weight='bold'>$MINUTES minutes</span>" \
    --text-align=center \
    --width=500 \
    --height=160 \
    --borders=24 \
    --fixed \
    --no-buttons \
    --on-top \
    --center \
    --skip-taskbar \
    --timeout="$((MINUTES * 60))" \
    --timeout-indicator=bottom &
YAD_PID=$!

DELAY_SECONDS=$((MINUTES * 60))
CHECK_SECONDS=3

sleep "$CHECK_SECONDS"

if ! kill -0 "$YAD_PID" 2>/dev/null; then
    log "Failed to display shutdown warning: user=$USER_NAME"
    echo "Error: Failed to display the shutdown warning." >&2
    wait "$YAD_PID" 2>/dev/null || true
    exit 1
fi

log "Shutdown warning displayed: user=$USER_NAME pid=$YAD_PID"

sleep "$((DELAY_SECONDS - CHECK_SECONDS))"

log "Shutdown delay elapsed; powering off system"
systemctl poweroff
