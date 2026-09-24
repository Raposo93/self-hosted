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

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "Error: Docker Compose is required to validate service definitions." >&2
    exit 1
fi

PROJECT_PYTHON="python3"
if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    PROJECT_PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif ! command -v "$PROJECT_PYTHON" >/dev/null 2>&1; then
    echo "Error: 'python3' is required when .venv/bin/python is unavailable." >&2
    exit 1
fi

PYTHON_CACHE_DIRECTORY=""
UNIT_DIRECTORY=""
COMPOSE_DIRECTORY=""
cleanup() {
    if [[ -n "$COMPOSE_DIRECTORY" ]]; then
        rm -rf -- "$COMPOSE_DIRECTORY"
    fi
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

mapfile -d '' -t COMPOSE_FILES < <(
    git ls-files -z --cached --others --exclude-standard \
        '*/docker-compose.yml' '*/docker-compose.yaml' \
        '*/compose.yml' '*/compose.yaml'
)
if ((${#COMPOSE_FILES[@]} > 0)); then
    echo "Checking Docker Compose definitions..."
    for compose_file in "${COMPOSE_FILES[@]}"; do
        component_directory="${compose_file%/*}"
        example_file="$component_directory/.env.example"
        if [[ ! -f "$example_file" ]]; then
            echo "Error: $compose_file has no .env.example for safe validation." >&2
            exit 1
        fi
        echo "  $compose_file"
        COMPOSE_DIRECTORY="$(mktemp -d /tmp/self-hosted-compose.XXXXXX)"
        compose_name="${compose_file##*/}"
        cp -- "$compose_file" "$COMPOSE_DIRECTORY/$compose_name"
        for environment_example in "$example_file" "$component_directory"/.*.env.example; do
            [[ -f "$environment_example" ]] || continue
            environment_name="${environment_example##*/}"
            cp -- "$environment_example" "$COMPOSE_DIRECTORY/${environment_name%.example}"
        done
        if ! env -i PATH="$PATH" docker compose \
            --env-file "$COMPOSE_DIRECTORY/.env" \
            -f "$COMPOSE_DIRECTORY/$compose_name" config --quiet; then
            echo "Error: Docker Compose validation failed for $compose_file." >&2
            exit 1
        fi
        rm -rf -- "$COMPOSE_DIRECTORY"
        COMPOSE_DIRECTORY=""
    done
fi

mapfile -d '' -t SHELL_SCRIPTS < <(
    git ls-files -z --cached --others --exclude-standard '*.sh'
)
if ((${#SHELL_SCRIPTS[@]} > 0)); then
    echo "Checking Bash syntax..."
    for script in "${SHELL_SCRIPTS[@]}"; do
        if ! bash -n "$script"; then
            echo "Error: Bash syntax check failed for $script." >&2
            exit 1
        fi
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
    if [[ -d mikrotik-report/tests ]]; then
        "$PROJECT_PYTHON" -m unittest discover -s mikrotik-report/tests -v
    fi
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
