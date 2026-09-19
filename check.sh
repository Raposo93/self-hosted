#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

for command in git bash; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Error: '$command' is required." >&2
        exit 1
    fi
done

PROJECT_PYTHON="python3"
if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    PROJECT_PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif ! command -v "$PROJECT_PYTHON" >/dev/null 2>&1; then
    echo "Error: 'python3' is required when .venv/bin/python is unavailable." >&2
    exit 1
fi

PYTHON_CACHE_DIRECTORY=""
UNIT_DIRECTORY=""
cleanup() {
    if [[ -n "$PYTHON_CACHE_DIRECTORY" ]]; then
        rm -rf -- "$PYTHON_CACHE_DIRECTORY"
    fi
    if [[ -n "$UNIT_DIRECTORY" ]]; then
        rm -rf -- "$UNIT_DIRECTORY"
    fi
}
trap cleanup EXIT

echo "Checking Git changes and conflict markers..."
git --no-pager diff --check
git --no-pager diff --cached --check
if git --no-pager grep -nE '^(<<<<<<< .+|=======|>>>>>>> .+)$'; then
    echo "Error: unresolved merge conflict markers found." >&2
    exit 1
fi

mapfile -d '' -t SHELL_SCRIPTS < <(
    git ls-files -z --cached --others --exclude-standard '*.sh'
)
if ((${#SHELL_SCRIPTS[@]} > 0)); then
    echo "Checking Bash syntax..."
    for script in "${SHELL_SCRIPTS[@]}"; do
        bash -n "$script"
    done
    if command -v shellcheck >/dev/null 2>&1; then
        echo "Running shellcheck..."
        shellcheck -- "${SHELL_SCRIPTS[@]}"
    else
        echo "Skipping shellcheck (not installed)."
    fi
fi

mapfile -d '' -t PYTHON_SCRIPTS < <(
    git ls-files -z --cached --others --exclude-standard '*.py'
)
if ((${#PYTHON_SCRIPTS[@]} > 0)); then
    echo "Checking Python syntax and running tests..."
    PYTHON_CACHE_DIRECTORY="$(mktemp -d /tmp/self-hosted-pycache.XXXXXX)"
    PYTHONPYCACHEPREFIX="$PYTHON_CACHE_DIRECTORY"
    export PYTHONPYCACHEPREFIX
    "$PROJECT_PYTHON" -m py_compile "${PYTHON_SCRIPTS[@]}"
    "$PROJECT_PYTHON" -m unittest discover -s mikrotik-report/tests -v
    echo "Checking Python lint, formatting, and types..."
    "$PROJECT_PYTHON" -m ruff check .
    "$PROJECT_PYTHON" -m ruff format --check .
    "$PROJECT_PYTHON" -m pyright
fi

mapfile -d '' -t SYSTEMD_UNITS < <(
    git ls-files -z --cached --others --exclude-standard \
        '*.service' '*.service.example' '*.timer' '*.timer.example'
)
if ((${#SYSTEMD_UNITS[@]} > 0)); then
    if command -v systemd-analyze >/dev/null 2>&1; then
        echo "Verifying systemd units..."
        UNIT_DIRECTORY="$(mktemp -d /tmp/self-hosted-systemd.XXXXXX)"
        UNIT_PATHS=()
        for unit in "${SYSTEMD_UNITS[@]}"; do
            unit_name="${unit##*/}"
            unit_path="$UNIT_DIRECTORY/${unit_name%.example}"
            cp -- "$unit" "$unit_path"
            UNIT_PATHS+=("$unit_path")
        done
        systemd-analyze verify "${UNIT_PATHS[@]}"
    else
        echo "Skipping systemd unit verification (systemd-analyze not installed)."
    fi
fi

echo "All available checks passed."
