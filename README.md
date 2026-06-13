# self-hosted

Personal self-hosted services, deployment files and maintenance scripts.

This repository contains Docker Compose setups and helper scripts used to run and maintain services on a private server.

## Contents

| Path                         | Purpose                               |
| ---------------------------- | ------------------------------------- |
| `filebrowser/`               | File Browser deployment               |
| `firefly/`                   | Firefly III deployment                |
| `grocy/`                     | Grocy deployment                      |
| `jellyfin/`                  | Jellyfin deployment                   |
| `navidrome/`                 | Navidrome deployment                  |
| `paperless-ngx/`             | Paperless-ngx deployment              |
| `qBittorrent-nox/`           | qBittorrent-nox deployment            |
| `vikunja/`                   | Vikunja deployment and helper scripts |
| `wikijs/`                    | Wiki.js deployment                    |
| `acme_cerificate_installer/` | ACME certificate installation helper  |
| `pbc/`                       | Proxmox Backup Client scripts         |

## Repository conventions

Runtime data, local databases, generated files, credentials and secrets should not be committed.

Most services are configured through local environment files or service-specific configuration files. When a sample file is provided, copy it and adapt it locally.

Example:

```bash
cp sample.env .env
```

## Security notes

Do not commit:

* `.env` files
* credentials
* API tokens
* private keys
* generated certificates
* database files
* service runtime data
* logs

This repository is intended to store deployment definitions and reusable scripts, not live private data.

## Usage

Enter the directory of the service or tool you want to manage and review its files before running anything.

For Docker Compose based services:

```bash
cd service-name
docker compose up -d
```

For scripts:

```bash
cd script-directory
chmod +x script-name.sh
./script-name.sh
```

Some scripts may require root permissions, systemd, local credentials or extra configuration.

## Backups

Backup-related scripts live under `pbc/`.

Review `pbc/sample.env`, copy it to `.env`, adapt the values and keep `.env` untracked.

## Notes

This is a personal infrastructure repository. It is optimized for maintainability and recovery, not for being a generic production-ready template.
