#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: setup-ssh-host.sh <alias> <user> <host-or-ip>

Create or reuse an Ed25519 key, copy it to the remote host, and manage the
corresponding entry in ~/.ssh/config.

Example:
    setup-ssh-host.sh gserver gonzalo 10.1.1.11
USAGE
}

if [[ $# -ne 3 ]]; then
    usage >&2
    exit 2
fi

host_alias=$1
remote_user=$2
remote_host=$3

if [[ ! "$host_alias" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "Error: alias may only contain letters, numbers, dots, underscores, and hyphens." >&2
    exit 2
fi

if [[ ! "$remote_user" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "Error: user may only contain letters, numbers, dots, underscores, and hyphens." >&2
    exit 2
fi

if [[ -z "$remote_host" || "$remote_host" =~ [[:space:]] ]]; then
    echo "Error: host or IP must not be empty or contain whitespace." >&2
    exit 2
fi

for command in ssh ssh-keygen ssh-copy-id awk grep mktemp; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Error: required command not found: $command" >&2
        exit 1
    fi
done

ssh_dir="$HOME/.ssh"
ssh_config="$ssh_dir/config"
key_file="$ssh_dir/id_ed25519_${host_alias}"
public_key_file="${key_file}.pub"
config_key_file="$HOME/.ssh/id_ed25519_${host_alias}"
begin_marker="# BEGIN self-hosted ssh-client-setup: ${host_alias}"
end_marker="# END self-hosted ssh-client-setup: ${host_alias}"

mkdir -p "$ssh_dir"
chmod 700 "$ssh_dir"
touch "$ssh_config"
chmod 600 "$ssh_config"

has_begin=false
has_end=false

grep -Fqx "$begin_marker" "$ssh_config" && has_begin=true
grep -Fqx "$end_marker" "$ssh_config" && has_end=true

if [[ "$has_begin" != "$has_end" ]]; then
    echo "Error: incomplete managed block found for '$host_alias' in $ssh_config." >&2
    exit 1
fi

host_count=$(awk -v alias="$host_alias" '
    tolower($1) == "host" && NF == 2 && $2 == alias { count++ }
    END { print count + 0 }
' "$ssh_config")

if [[ "$has_begin" == false && "$host_count" -gt 0 ]]; then
    echo "Error: an unmanaged Host '$host_alias' entry already exists in $ssh_config." >&2
    echo "Refusing to overwrite it." >&2
    exit 1
fi

if [[ "$has_begin" == true && "$host_count" -ne 1 ]]; then
    echo "Error: multiple Host '$host_alias' entries exist in $ssh_config." >&2
    echo "Resolve the duplicate entries before running this script again." >&2
    exit 1
fi

if [[ -f "$key_file" ]]; then
    echo "Using existing private key: $key_file"
else
    echo "Creating Ed25519 key: $key_file"
    ssh-keygen -t ed25519 -f "$key_file" -C "$host_alias"
fi
chmod 600 "$key_file"

if [[ -f "$public_key_file" ]]; then
    echo "Using existing public key: $public_key_file"
else
    echo "Rebuilding missing public key: $public_key_file"
    ssh-keygen -y -f "$key_file" > "$public_key_file"
fi
chmod 644 "$public_key_file"

echo "Copying public key to ${remote_user}@${remote_host}"
ssh-copy-id -i "$public_key_file" "${remote_user}@${remote_host}"

tmp_config=$(mktemp "${ssh_config}.XXXXXX")
trap 'rm -f "$tmp_config"' EXIT

if [[ "$has_begin" == true ]]; then
    awk -v begin="$begin_marker" -v end="$end_marker" '
        $0 == begin { skip = 1; next }
        $0 == end { skip = 0; next }
        !skip { lines[++n] = $0 }
        END {
            while (n > 0 && lines[n] == "") {
                n--
            }
            for (i = 1; i <= n; i++) {
                print lines[i]
            }
        }
    ' "$ssh_config" > "$tmp_config"
else
    cat "$ssh_config" > "$tmp_config"
fi

{
    if [[ -s "$tmp_config" ]]; then
        cat "$tmp_config"
        printf '\n'
    fi

    cat <<EOF_CONFIG
$begin_marker
Host $host_alias
    HostName $remote_host
    User $remote_user
    IdentityFile $config_key_file
$end_marker
EOF_CONFIG
} > "$ssh_config"

chmod 600 "$ssh_config"
ssh -G "$host_alias" >/dev/null

rm -f "$tmp_config"
trap - EXIT

echo "SSH host '$host_alias' configured successfully."
echo "Test it with: ssh $host_alias"
