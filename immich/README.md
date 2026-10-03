# Immich

Docker Compose definition for the existing Immich deployment. It includes the
server, machine learning service, Valkey, and PostgreSQL. The external photo
library is mounted read-only at `/external/photos`.

Copy `.env.example` to `.env` and set the paths and database password. For an
existing installation, retain its current `UPLOAD_LOCATION`,
`EXTERNAL_LIBRARY_LOCATION`, and `DB_DATA_LOCATION`; changing these paths without
moving the data can make the library or database appear missing. Do not copy the
server's database or uploads into Git.

Immich container images are pinned to explicit versions in `docker-compose.yml`.
Renovate proposes image updates through pull requests; updates are reviewed and
deployed manually.

`IMMICH_BIND=0.0.0.0` and `IMMICH_PORT=2283` preserve the supplied Compose file's
port mapping. Set `IMMICH_BIND=127.0.0.1` if this host should serve Immich only
through a local reverse proxy.

After adapting `.env`, validate the configuration with `docker compose config`
from this directory. Starting or replacing the live containers is a separate
deployment step. Back up the database and uploaded files before upgrading
Immich; the external photo library is a separate read-only mount.