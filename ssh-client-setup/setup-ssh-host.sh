#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: setup-ssh-host.sh <alias> <user> <host-or-ip>

Create or reuse an Ed25519 key, copy it to the remote host, and manage the
corresponding entry in ~/.ssh/config.

Example:
    setup-ssh-host.sh example-host admin 192.0.2.10
USAGE
}

if [[ $# -ne 3 ]]; then
    usage >&2
    exit 2
fi

host_alias=$1
remote_user=$2
remote_host=$3

if [[ ! "$host_alias" =~ ^[A-Za-z0-9._]+[A-Za-z0-9._-]*$ ]]; then
    echo "Error: alias may only contain letters, numbers, dots, underscores, and hyphens." >&2
    exit 2
fi

if [[ ! "$remote_user" =~ ^[A-Za-z0-9._]+[A-Za-z0-9._-]*$ ]]; then
    echo "Error: user may only contain letters, numbers, dots, underscores, and hyphens." >&2
    exit 2
fi

if [[ ! "$remote_host" =~ ^[A-Za-z0-9._:]+[A-Za-z0-9._:-]*$ ]]; then
    echo "Error: host or IP may only contain letters, numbers, dots, underscores, colons, and hyphens, and must not start with a hyphen." >&2
    exit 2
fi

for command in ssh ssh-keygen ssh-copy-id awk grep mktemp cmp mv; do
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

if [[ -e "$ssh_config" && ! -f "$ssh_config" || -L "$ssh_config" ]]; then
    echo "Error: $ssh_config must be a regular file, not a symlink." >&2
    exit 1
fi

if [[ "$config_key_file" == *'"'* || "$config_key_file" == *'\'* || "$config_key_file" == *'%'* || "$config_key_file" == *$'\n'* ]]; then
    echo "Error: home directory path cannot be represented safely in SSH config." >&2
    exit 1
fi

original_config=$(mktemp "${ssh_config}.original.XXXXXX")
tmp_config=$(mktemp "${ssh_config}.candidate.XXXXXX")
trap 'rm -f -- "$original_config" "$tmp_config"' EXIT

config_existed=false
if [[ -f "$ssh_config" ]]; then
    config_existed=true
    cat "$ssh_config" > "$original_config"
fi

begin_count=$(grep -Fxc "$begin_marker" "$original_config" || true)
end_count=$(grep -Fxc "$end_marker" "$original_config" || true)

if (( begin_count > 1 || end_count > 1 || begin_count != end_count )); then
    echo "Error: incomplete or duplicate managed block found for '$host_alias' in $ssh_config." >&2
    exit 1
fi

if ! awk -v begin="$begin_marker" -v end="$end_marker" '
    $0 == begin { if (skip) exit 1; skip = 1; next }
    $0 == end { if (!skip) exit 1; skip = 0; next }
    !skip { lines[++count] = $0 }
    END {
        if (skip) exit 1
        while (count > 0 && lines[count] == "") count--
        for (i = 1; i <= count; i++) print lines[i]
    }
' "$original_config" > "$tmp_config"; then
    echo "Error: malformed managed block in $ssh_config." >&2
    exit 1
fi

host_count=$(awk -v alias="$host_alias" '
    tolower($1) == "host" {
        for (i = 2; i <= NF; i++) if ($i == alias) count++
    }
    END { print count + 0 }
' "$tmp_config")

if [[ "$host_count" -gt 0 ]]; then
    echo "Error: an unmanaged Host '$host_alias' entry already exists in $ssh_config." >&2
    echo "Refusing to overwrite it." >&2
    exit 1
fi

if [[ -s "$tmp_config" ]]; then
    printf '\n' >> "$tmp_config"
fi
cat >> "$tmp_config" <<EOF_CONFIG
$begin_marker
Host $host_alias
    HostName $remote_host
    User $remote_user
    IdentityFile "$config_key_file"
$end_marker
EOF_CONFIG
chmod 600 "$tmp_config"

if ! effective_config=$(ssh -G -F "$tmp_config" -- "$host_alias"); then
    echo "Error: candidate SSH configuration is invalid; $ssh_config was not changed." >&2
    exit 1
fi

if ! printf '%s\n' "$effective_config" | awk -v host="$remote_host" -v user="$remote_user" -v identity="$config_key_file" '
    $1 == "hostname" { actual_host = substr($0, length($1) + 2) }
    $1 == "user" { actual_user = substr($0, length($1) + 2) }
    $1 == "identityfile" && substr($0, length($1) + 2) == identity { found_identity = 1 }
    END {
        if (actual_host != host || actual_user != user || !found_identity) {
            printf "Error: effective SSH settings conflict with requested host/user/identity (hostname=%s, user=%s, identity present=%s).\n", actual_host, actual_user, found_identity ? "yes" : "no" > "/dev/stderr"
            exit 1
        }
    }
'; then
    echo "Resolve earlier Host, Match, or Include rules manually; $ssh_config was not changed." >&2
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

if [[ "$config_existed" == true ]]; then
    if [[ ! -f "$ssh_config" || -L "$ssh_config" ]] || ! cmp -s -- "$original_config" "$ssh_config"; then
        echo "Error: $ssh_config changed during setup; refusing to replace it." >&2
        exit 1
    fi
elif [[ -e "$ssh_config" || -L "$ssh_config" ]]; then
    echo "Error: $ssh_config appeared during setup; refusing to replace it." >&2
    exit 1
fi

mv -- "$tmp_config" "$ssh_config"

echo "SSH host '$host_alias' configured successfully."
echo "Test it with: ssh $host_alias"
