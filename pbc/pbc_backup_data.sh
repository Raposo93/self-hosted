#!/usr/bin/env bash
set -Eeuo pipefail

exec python3 "$(dirname -- "${BASH_SOURCE[0]}")/pbc_backup_data.py" "$@"
