# HAProxy

HAProxy publishes HTTP services over HTTPS. The host map is the source of truth
for published names and for [`acme_haproxy`](../acme_haproxy/README.md), which
issues and renews one HTTP-01 certificate per name. The steps below install a
new Debian/Ubuntu host with an initially empty certificate directory.

## Before starting

Use a real domain you control in place of `example.com`. Its public A record
(and AAAA record, if present) must resolve to this host's reachable address.
Forward inbound TCP 80 and 443 to this host and allow them through its firewall.
HTTP-01 validation must reach port 80 from the public internet; a local curl
alone does not prove this. Remove or correct an AAAA record if IPv6 is not
reachable. Ensure no other service occupies ports 80, 443, or 8888 on this host.
The upstream application must also be reachable at the address configured in
its HAProxy backend; the example uses `127.0.0.1:8080`.

Run repository-relative commands from the repository root:

```bash
cd /path/to/self-hosted
```

The installed paths are `/etc/haproxy/haproxy.cfg`,
`/etc/haproxy/maps/hosts.map`, `/etc/haproxy/certs/acme/`, and
`/var/www/acme-challenges/`. The ACME home is `/var/lib/acme-haproxy` in this
guide. Keep its account data and generated keys outside Git.

## 1. Install HAProxy and the local challenge server

```bash
sudo apt update
sudo apt install haproxy python3 git curl openssl ca-certificates
sudo install -d -m 0755 /etc/haproxy/maps /etc/haproxy/certs
sudo install -d -m 0700 /etc/haproxy/certs/acme
sudo install -d -m 0755 /var/www/acme-challenges
sudo install -m 0644 haproxy/acme-challenge-http.service.example \
  /etc/systemd/system/acme-challenge-http.service
sudo systemctl daemon-reload
sudo systemctl enable --now acme-challenge-http.service
sudo systemctl status acme-challenge-http.service
```

The unit runs Python's static HTTP server as `www-data`, bound only to
`127.0.0.1:8888`. HAProxy exposes only the challenge path to the public. Root
owns the webroot and writes challenge files; `www-data` needs read and directory
traversal access. Keep `/var/www/acme-challenges` at `0755` and ensure the
challenge files are world-readable (normally `0644`). The ACME home and PEM
directory remain root-only; the `haproxy` worker does not need to read the PEM
directly because the HAProxy master loads it before dropping privileges.

## 2. Start HAProxy without a certificate

Copy the HTTP-only configuration. It serves challenges and returns 404 for
other requests until TLS is ready. Do not install the HTTPS example while the
certificate directory is empty.

```bash
sudo install -m 0644 haproxy/haproxy-http-bootstrap.cfg.example \
  /etc/haproxy/haproxy.cfg
sudo install -m 0644 haproxy/hosts.map.example /etc/haproxy/maps/hosts.map
sudoedit /etc/haproxy/maps/hosts.map
sudo haproxy -c -f /etc/haproxy/haproxy.cfg
sudo systemctl enable haproxy
sudo systemctl restart haproxy
sudo systemctl status haproxy
```

In the map, replace the sample entries with **one** real hostname and its
backend, for example `example.com example_backend`. The backend named there
must exist in the final configuration in step 5. Keep only the first hostname
until it has a certificate. The bootstrap configuration does not route normal
application traffic.

Verify the complete challenge path before issuing a certificate:

```bash
sudo install -d -m 0755 /var/www/acme-challenges/.well-known/acme-challenge
printf 'acme-webroot-ok\n' | sudo tee \
  /var/www/acme-challenges/.well-known/acme-challenge/bootstrap-check
sudo chmod 0644 \
  /var/www/acme-challenges/.well-known/acme-challenge/bootstrap-check
```

From outside the host and its LAN (for example, on a mobile connection), run:

```bash
curl --fail --show-error \
  http://example.com/.well-known/acme-challenge/bootstrap-check
```

The response must be `acme-webroot-ok`. This also checks public DNS, port
forwarding, and firewall rules. After that check, remove the test file on the
HAProxy host:

```bash
sudo rm /var/www/acme-challenges/.well-known/acme-challenge/bootstrap-check
```

If the public request fails, check
`journalctl -u acme-challenge-http.service -u haproxy -n 100` and correct
networking or permissions before continuing.

## 3. Install `acme.sh` without its cron job

The helper runs as root and expects `acme.sh` at
`/var/lib/acme-haproxy/.acme.sh/acme.sh`. Install from the [upstream Git
repository](https://github.com/acmesh-official/acme.sh/wiki/How-to-install)
with `--no-cron`; the repository's systemd timer will handle renewal. Substitute
an address you control for the contact email. From a temporary working
directory:

```bash
sudo install -d -m 0700 /var/lib/acme-haproxy
cd /tmp
git clone --depth 1 https://github.com/acmesh-official/acme.sh.git acme-sh-source
cd acme-sh-source
sudo env HOME=/var/lib/acme-haproxy ./acme.sh --install --no-cron \
  --no-profile -m admin@example.com
sudo env HOME=/var/lib/acme-haproxy \
  /var/lib/acme-haproxy/.acme.sh/acme.sh \
  --set-default-ca --server letsencrypt
cd /path/to/self-hosted
```

Do not enable an additional `acme.sh --cron` entry or a second certificate
renewal timer. On a reused host, inspect root's crontab and existing timers
before enabling the timer in step 6; remove or disable an old ACME renewal job
only after confirming which certificates it owns.

## 4. Issue the first certificate

The domain must already be in `hosts.map`. The helper calls `acme.sh --issue`
with the HTTP-01 webroot, installs its full chain and private key, builds
`/etc/haproxy/certs/acme/<domain>.pem` with mode `0600`, validates the active
HAProxy configuration, and reloads it. During bootstrap that active
configuration is still HTTP-only.

```bash
sudo env HOME=/var/lib/acme-haproxy \
  python3 acme_haproxy/acme_haproxy.py issue example.com
sudo test -s /etc/haproxy/certs/acme/example.com.pem
sudo haproxy -c -f /etc/haproxy/haproxy.cfg
```

If issuance fails, keep HTTP running and inspect the command output and
`journalctl -u acme-challenge-http.service -u haproxy -n 100`. Do not create
a placeholder certificate. See the [ACME recovery notes](../acme_haproxy/README.md#pending-work-and-recovery)
if PEM deployment or reload fails.

## 5. Activate HTTPS

The final example binds port 443, redirects other HTTP requests to HTTPS,
routes names through the map, and keeps HTTP-01 requests on port 80. It contains
`example_backend` for `127.0.0.1:8080` and `remote_backend` for a sample remote
service. Edit backend addresses for your services; each backend named in the
map must exist. HAProxy serves 404 for unknown hostnames.

This example assumes clients connect directly to HAProxy. On its TLS-only
frontend, HAProxy replaces any client-supplied `X-Forwarded-For` with the IP
it observes (`src`) and `X-Forwarded-Proto` with `https`. It removes the
client-supplied `Forwarded` and `X-Real-IP` alternatives. Configure each
application to trust these proxy headers only from its HAProxy peer address or
the smallest network containing the HAProxy instances. Do not trust arbitrary
clients or all private networks. Keep a local backend on loopback where
possible; firewall a remote backend so only HAProxy can reach it. If an
application uses additional identity or scheme headers, remove or set them
explicitly too. With another proxy in front, `src` would be that proxy and
the transport seen here may differ from the client's; define and validate a
separate trusted-hop policy before using that topology.

```bash
sudo install -m 0644 haproxy/haproxy.cfg.example \
  /etc/haproxy/haproxy.cfg.https-candidate
sudoedit /etc/haproxy/haproxy.cfg.https-candidate
sudo haproxy -c -f /etc/haproxy/haproxy.cfg.https-candidate
sudo install -m 0644 /etc/haproxy/haproxy.cfg.https-candidate \
  /etc/haproxy/haproxy.cfg
sudo haproxy -c -f /etc/haproxy/haproxy.cfg
sudo systemctl reload haproxy
```

Only run the install/reload commands after candidate validation succeeds. If
the installed validation or reload fails, restore the known HTTP configuration
and validate it before reloading:

```bash
sudo install -m 0644 haproxy/haproxy-http-bootstrap.cfg.example \
  /etc/haproxy/haproxy.cfg
sudo haproxy -c -f /etc/haproxy/haproxy.cfg
sudo systemctl reload haproxy
```

If HAProxy stopped instead of accepting a reload, use
`sudo systemctl start haproxy` after the restored configuration validates.
Keep port 80 and the challenge server available while troubleshooting TLS.

Check the first name and HTTP redirection from an external client:

```bash
curl --show-error --verbose https://example.com/
curl --head http://example.com/
sudo systemctl status haproxy
```

The HTTPS request must complete with a trusted certificate for the hostname;
an application-specific HTTP error can still indicate an upstream problem.
The HTTP response should redirect to HTTPS. Do not use `curl -k` to claim a
production certificate is valid.

## 6. Enable renewal

Follow the [ACME systemd instructions](../acme_haproxy/README.md#systemd) to
install, test, and enable `acme-haproxy.service` and
`acme-haproxy.timer`. Set its `HOME` to `/var/lib/acme-haproxy` and its
`WorkingDirectory` and script path to this checkout. Check the service result,
journal, and next scheduled timer run. The timer is the sole renewal scheduler
for certificates managed here.

## Adding a domain later

Add its backend to `/etc/haproxy/haproxy.cfg` and its hostname/backend pair to
`/etc/haproxy/maps/hosts.map`. Validate and reload HAProxy, then run
`sudo env HOME=/var/lib/acme-haproxy python3 acme_haproxy/acme_haproxy.py issue <domain>`
from the repository root. Verify HTTPS for the new name. Each hostname gets
its own PEM; HTTP-01 remains available for renewal.

## Validation in a disposable environment

For a public test hostname pointed at a disposable Debian/Ubuntu host, run
steps 1–5 using a fresh ACME home and choose the Let's Encrypt staging CA in
step 3 by replacing `--server letsencrypt` with `--server letsencrypt_test`.
The [staging CA](https://letsencrypt.org/docs/staging-environment/) issues
certificates that browsers do not trust, so validate its HTTPS response with
`curl -k` **only in this disposable test**. Confirm the PEM exists, HAProxy
validation and reload succeed, the challenge URL is public, and the renewal
service and timer work. Destroy the test host afterwards. Use a fresh ACME
home with the production CA for a real deployment; do not carry staging
certificates into production.

### Verification record (2026-10-06)

* `./check.sh` passed, including the ACME helper tests and systemd unit verification.
* The challenge unit separately passed `systemd-analyze verify`.
* The bootstrap path was exercised on a fresh Debian 13 unprivileged LXC with
  an initially empty HAProxy certificate directory.
* The local challenge service ran under systemd on `127.0.0.1:8888`, while
  HAProxy exposed the HTTP bootstrap frontend on port 80.
* The HTTP-01 challenge path was verified through HAProxy, and Let's Encrypt
  successfully issued the first certificate. No separate external `curl`
  check was performed; successful issuance confirmed public HTTP-01 reachability.
* The generated PEM was loaded by HAProxy, the HTTPS candidate passed
  `haproxy -c`, and HAProxy reloaded successfully with listeners on ports
  80 and 443 while the challenge service remained local on port 8888.
* A temporary backend on `127.0.0.1:8080` returned HTTP 200 through the
  HTTPS frontend and host map, validating TLS termination and backend routing.
* `acme-haproxy.service` completed successfully and correctly skipped the
  certificate because renewal was not due.
* `acme-haproxy.timer` was enabled and active, with the next daily renewal
  run scheduled by systemd.

### Forwarded-header verification (2026-10-05)

* A disposable HAProxy 3.2.9 instance passed `haproxy -c` using this example
  with temporary bind ports, certificate, host map, backend addresses, and
  host-specific paths.
* An echo backend received `X-Forwarded-For: 127.0.0.1` and
  `X-Forwarded-Proto: https` over TLS despite forged client values; the forged
  `Forwarded` and `X-Real-IP` headers were absent.
* HTTP still returned a 301 HTTPS redirect, while the ACME challenge path
  reached its HTTP backend. This local test did not change a live deployment
  or verify public certificate issuance.
