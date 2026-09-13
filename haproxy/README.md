# HAProxy

Standard HAProxy configuration for publishing HTTP services over HTTPS.

The configuration uses:

* HTTP on port `80`;
* HTTPS on port `443`;
* HTTP to HTTPS redirection;
* HTTP-01 ACME challenges;
* SNI certificates loaded from `/etc/haproxy/certs/acme`;
* hostname-to-backend routing through `/etc/haproxy/maps/hosts.map`;
* a fallback backend returning HTTP `404`.

Certificate issuance and renewal are managed by [`../acme_haproxy`](../acme_haproxy/README.md).

## Requirements

* HAProxy
* A local HTTP server listening on `127.0.0.1:8888` and serving `/var/www/acme-challenges`
* Certificates stored as PEM files under `/etc/haproxy/certs/acme`

This layout is intended for a standard Debian or Ubuntu HAProxy installation.

## Layout

The repository contains:

```text
haproxy/
├── README.md
├── haproxy.cfg.example
└── maps/
    └── hosts.map.example
```

The installed configuration uses:

```text
/etc/haproxy/haproxy.cfg
/etc/haproxy/maps/hosts.map
/etc/haproxy/certs/acme/
/var/www/acme-challenges/
```

## Install HAProxy

Install HAProxy using the distribution package:

```bash
sudo apt update
sudo apt install haproxy
```

Create the directories used by this configuration:

```bash
sudo install -d /etc/haproxy/maps
sudo install -d /etc/haproxy/certs/acme
sudo install -d /var/www/acme-challenges
```

Copy the configuration:

```bash
sudo cp haproxy.cfg.example /etc/haproxy/haproxy.cfg
```

Copy the host map:

```bash
sudo cp maps/hosts.map.example /etc/haproxy/maps/hosts.map
```

Edit both files for the services that will be published.

## Host map

The host map is the source of truth for published domains:

```text
example.com example_backend
service.example.com service_backend
```

The first field is the hostname received through HTTP.

The second field is the HAProxy backend that should handle requests for that hostname.

Blank lines and lines beginning with `#` may be used for readability.

The same map is read by [`../acme_haproxy`](../acme_haproxy/README.md) to determine which domains are managed for ACME certificate operations.

## Backends

Every backend referenced by `hosts.map` must exist in `haproxy.cfg`.

For a local service:

```text
backend example_backend
    mode http
    server example 127.0.0.1:8080
```

For a service running on another machine:

```text
backend remote_backend
    mode http
    server remote 10.0.0.10:8080
```

Requests for hostnames that do not exist in the map are sent to `unknown_backend`, which returns HTTP `404`.

## HTTP-01 challenges

Requests beginning with:

```text
/.well-known/acme-challenge/
```

are not redirected to HTTPS.

They are sent to:

```text
acme_backend
```

which expects an HTTP server on:

```text
127.0.0.1:8888
```

serving:

```text
/var/www/acme-challenges
```

This matches the webroot used by `acme_haproxy` when issuing certificates.

All other HTTP requests are redirected permanently to HTTPS.

## Certificates

HAProxy loads PEM certificates from:

```text
/etc/haproxy/certs/acme
```

Each managed domain uses:

```text
/etc/haproxy/certs/acme/<domain>.pem
```

The PEM contains the full certificate chain followed by the private key.

`acme_haproxy` creates these files with permissions `0600`.

### Initial bootstrap

The HTTPS frontend requires at least one usable certificate before HAProxy can start with the complete TLS configuration.

On a completely new installation with an empty certificate directory, bootstrap the first certificate before enabling the final HTTPS frontend, or start with a temporary HTTP-only configuration that exposes the ACME challenge path.

Once the first certificate exists, normal certificate issuance is performed with:

```bash
sudo HOME=/path/to/acme-user-home \
  python3 ../acme_haproxy/acme_haproxy.py issue example.com
```

After bootstrap, no manual `acme.sh --issue`, certificate installation, or PEM construction is required.

## Publishing a service

To publish a new service:

1. add its backend to `/etc/haproxy/haproxy.cfg`;
2. add its domain and backend to `/etc/haproxy/maps/hosts.map`;
3. validate the HAProxy configuration;
4. reload HAProxy if the routing configuration changed;
5. issue the certificate with `acme_haproxy.py issue <domain>`;
6. verify HTTPS.

Example map entry:

```text
service.example.com service_backend
```

Example backend:

```text
backend service_backend
    mode http
    server service 127.0.0.1:8080
```

Issue the certificate:

```bash
sudo HOME=/path/to/acme-user-home \
  python3 ../acme_haproxy/acme_haproxy.py issue service.example.com
```

## Validate the configuration

Always validate the configuration before reloading HAProxy:

```bash
sudo haproxy -c -f /etc/haproxy/haproxy.cfg
```

If validation succeeds:

```bash
sudo systemctl reload haproxy
```

`acme_haproxy` performs this validation automatically before reloading HAProxy after certificate operations.

## Service management

Check HAProxy:

```bash
sudo systemctl status haproxy
```

View recent logs:

```bash
journalctl -u haproxy -n 100
```

Follow logs:

```bash
journalctl -u haproxy -f
```
