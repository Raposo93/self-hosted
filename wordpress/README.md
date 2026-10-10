# WordPress

`docker-compose.yml` runs WordPress with Apache and a dedicated MariaDB
database. Docker Engine and Docker Compose are required. Images use explicit
versions; Renovate proposes updates for manual review and deployment.

## Configuration and installation

From the repository root, enter this directory and create the local configuration:

```bash
cd wordpress
cp .env.example .env
chmod 600 .env
```

Edit `.env` before starting the service. Replace both password placeholders
with strong, distinct passwords and review these values:

| Variable | Purpose |
| --- | --- |
| `MYSQL_ROOT_PASSWORD` | MariaDB root password for initial database setup |
| `WORDPRESS_DB_NAME` | Application database name |
| `WORDPRESS_DB_USER` | Application database user |
| `WORDPRESS_DB_PASSWORD` | Application database password |
| `WORDPRESS_BIND` | Published HTTP bind address; defaults to loopback |
| `WORDPRESS_PORT` | Published HTTP port; defaults to `8088` |
| `DB_DATA_DIR` | MariaDB data directory; defaults to `./db` |
| `DATA_DIR` | WordPress files, including uploads, themes, plugins and configuration; defaults to `./html` |

Relative data paths resolve against this directory. Keep existing paths when
updating an installation. The default `db/`, `html/` and local `.env` are
ignored by Git; custom paths must also remain outside tracked files.

Validate locally, then start the containers on the intended deployment host:

```bash
docker compose config --quiet
docker compose up -d
docker compose ps
```

Avoid sharing the full output of `docker compose config`, which includes
resolved passwords. MariaDB initializes a new database on its first start;
changing environment passwords afterwards does not rotate credentials in an
existing database. Coordinate any credential change with the database itself.

`depends_on` orders container startup but does not wait for database readiness.
The initial database initialization can take time; inspect logs if WordPress
reports a database connection error. There are no configured healthchecks, so
an `Up` status alone does not establish application health.

## HAProxy and HTTPS

The default port is accessible only from the same host at
`http://127.0.0.1:8088`. MariaDB has no published host port. For HTTPS, follow
the [HAProxy guide](../haproxy/README.md) and its ACME workflow. Add a backend
pointing to `127.0.0.1:8088` (or the configured port) to the installed HAProxy
configuration, and map the site's hostname to that backend in the existing
HAProxy host map. The map remains the domain inventory for certificate
management.

This loopback configuration assumes HAProxy runs on the Docker host. A remote
proxy requires an intentional bind-address change and firewall restrictions
so only that proxy can reach the backend.

The official image's generated [`wp-config.php`](https://github.com/docker-library/wordpress/blob/master/wp-config-docker.php)
recognizes `X-Forwarded-Proto: https`, which the repository's HAProxy example
sets. If reusing a custom `wp-config.php`, verify that it handles HTTPS behind
the proxy too. Complete the WordPress installation through the final HTTPS
hostname and verify that both the site and administration pages work without
redirect loops or mixed content. Keep backend access restricted because the
generated configuration trusts the forwarded scheme header.

## Logs and recovery

```bash
docker compose logs --tail=100 wordpress db
```

For database errors, check MariaDB initialization and that the application
credentials match the existing database. For missing files or permission
errors, check the configured bind paths and container write access. Do not
delete data directories to resolve a startup failure.

Back up both the database and WordPress files, and keep the private `.env`
available for recovery. Use a logical database dump or a cold copy taken with
the services stopped; copying a running MariaDB data directory is not a
consistent backup procedure. Coordinate database and file backups to avoid
changes between them. Existing [PBC tooling](../pbc/README.md) supports
pre/post hooks for preparing database dumps; keep dumps and private hook
configuration outside Git.

Before an upgrade, retain a recoverable backup of both data sets and record
the previous image versions. Test recovery in a separate instance with
separate data directories and container names (the Compose names are fixed).
Verify content, uploads and administrator login there. A database migration
may require restoring the pre-upgrade backup rather than merely reverting an
image tag. Repository changes do not deploy or restart this service.
