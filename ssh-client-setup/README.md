# SSH Client Setup

Small Bash helper for provisioning a per-host SSH key and the corresponding
client configuration on an administration workstation.

The script is intentionally client-side only. It does not configure `sshd` or
change server SSH policy.

## What it does

* creates `~/.ssh` when needed and applies mode `700`;
* creates an Ed25519 key named `~/.ssh/id_ed25519_<alias>` when it does not exist;
* reuses an existing key instead of replacing it;
* copies the public key with `ssh-copy-id`;
* adds a managed `Host` block to `~/.ssh/config`;
* updates only blocks previously created by this script;
* refuses to overwrite an existing unmanaged block for the same alias;
* applies mode `600` to the private key and SSH config, and `644` to the public key;
* validates the resulting client configuration with `ssh -G`.

## Requirements

The workstation must provide:

* `bash`
* `ssh`
* `ssh-keygen`
* `ssh-copy-id`
* `awk`
* `grep`
* `mktemp`

The remote SSH service is expected to use the default port `22`.

The first connection must also have a working authentication method so
`ssh-copy-id` can install the new public key, normally the remote user's
password or another already-authorized SSH key.

## Usage

```bash
./setup-ssh-host.sh <alias> <user> <host-or-ip>
```

Example:

```bash
./setup-ssh-host.sh example-host admin 192.0.2.10
```

This creates or reuses:

```text
~/.ssh/id_ed25519_example-host
~/.ssh/id_ed25519_example-host.pub
```

and manages this block in `~/.ssh/config`:

```text
# BEGIN self-hosted ssh-client-setup: example-host
Host example-host
    HostName 192.0.2.10
    User admin
    IdentityFile ~/.ssh/id_ed25519_example-host
# END self-hosted ssh-client-setup: example-host
```

Then connect with:

```bash
ssh example-host
```

## Existing keys and config

The script never replaces an existing private key.

If the private key exists but its `.pub` file is missing, the public key is
rebuilt from the private key.

A managed block can be updated by running the script again with the same alias.
For example, this can update the IP address after a host moves.

If an exact `Host <alias>` entry already exists but was not created by this
script, execution stops so manual SSH configuration is not silently replaced.

## Interactive prompts

`ssh-keygen` may ask for a passphrase when creating a new key.

`ssh-copy-id` may ask for confirmation of the server host key and for the
remote user's password. Those prompts are intentional.
