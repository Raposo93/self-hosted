# File Browser

This Compose service stores served files at `DATA_DIR`, settings in
`filebrowser_config/`, and the database in `filebrowser_database/`. The latter
two directories are local runtime data and are ignored by Git. The image's
default `/config/settings.json` uses `/database/filebrowser.db`.

## New installation

Copy `.env.example` to `.env` and set `DATA_DIR`, `PUID`, `PGID`, bind address,
and port. Create the local writable directories before starting the service:

```sh
mkdir -p filebrowser_config filebrowser_database
sudo chown 1000:1000 filebrowser_config filebrowser_database
chmod 700 filebrowser_config filebrowser_database
docker compose up -d
```

Replace `1000:1000` with the `PUID:PGID` values in `.env`. The selected user
also needs write access to `filebrowser_config/` and the intended access to
`DATA_DIR`. Keep the database and its backups out of Git.

## Persistence check

On a disposable instance, create a test user and change a setting, then run
`docker compose down` followed by `docker compose up -d` (without `--volumes`).
Confirm that the same user and setting remain.
