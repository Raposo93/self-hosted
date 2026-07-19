# Delayed Shutdown

Small Bash script that displays a graphical shutdown warning in an active Wayland session, waits for the requested number of minutes, and powers off the system.

## Requirements

- Bash
- systemd
- Wayland
- `yad`
- `runuser`
- An active graphical session for the target user
- Root privileges

## Usage

```bash
sudo ./shutdown-delayed.sh <minutes> <user_name> [wayland_display]
```

Examples:

```bash
sudo ./shutdown-delayed.sh 15 alice
```

```bash
sudo ./shutdown-delayed.sh 15 alice wayland-1
```

The optional Wayland display defaults to:

```text
wayland-0
```

## How it works

The script:

1. validates the arguments and required user;
2. checks that `yad` is installed;
3. verifies that the user's runtime directory, Wayland socket, and D-Bus socket exist;
4. displays a graphical shutdown warning in the user's session;
5. waits for the requested number of minutes;
6. runs `systemctl poweroff`.

Closing the warning window does not cancel the shutdown.

## Installation

Make the script executable:

```bash
chmod +x shutdown-delayed.sh
```

For scheduled root execution, install a root-owned copy:

```bash
sudo install -o root -g root -m 755   shutdown-delayed.sh   /usr/local/sbin/shutdown-delayed
```

This avoids running a root cron job from a script that can be modified by an unprivileged user.

## Cron example

Edit root's crontab:

```bash
sudo crontab -e
```

Schedule a warning at 23:00 followed by shutdown 15 minutes later:

```cron
0 23 * * * /usr/local/sbin/shutdown-delayed 15 alice
```

Replace `alice` with the user who owns the active graphical session.

Cron uses a minimal environment, so the script relies on the user's runtime sockets under `/run/user/<uid>` instead of the caller's desktop environment variables.

## Troubleshooting

### The script says it must run as root

Run it with `sudo` or from root's crontab.

### `yad` is not installed

Install YAD using your distribution's package manager.

### Wayland socket not found

Check the current Wayland display from the graphical user's terminal:

```bash
echo "$WAYLAND_DISPLAY"
```

Pass the returned value as the optional third argument.

### No active runtime directory found

The target user does not currently have an active session, or the session runtime directory is unavailable.

### The warning window does not appear

Confirm that:

- the target username is correct;
- the user has an active Wayland session;
- the Wayland display name is correct;
- the user's D-Bus session socket exists.

## Scope

This first version targets Linux systems using:

- systemd;
- Wayland;
- YAD.

X11 is not currently supported.

## Security

The script powers off the system and must run as root.

When using cron, prefer a root-owned copy under `/usr/local/sbin` instead of executing the script directly from a user-writable repository.

## License

No license is included yet. Add one before distributing the script for reuse.
