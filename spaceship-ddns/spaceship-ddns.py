import ipaddress
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import cast

PUBLIC_IP_URL = "https://api.ipify.org"
SPACESHIP_BASE_URL = "https://spaceship.dev/api/v1/dns/records"
TIMEOUT = 15


logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger("spaceship-ddns")


def _get_required_env(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")

    return value


def _http_request(
    url: str,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    data: object | None = None,
) -> str:
    request_headers = dict(headers or {})
    request_headers["User-Agent"] = "spaceship-ddns/1.0"

    body = None

    if data is not None:
        body = json.dumps(data).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url=url,
        method=method,
        headers=request_headers,
        data=body,
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read().decode("utf-8")

    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {exc.code} calling {method} {url}: {response_body}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Network error calling {method} {url}: {exc.reason}"
        ) from exc


def _get_public_ip() -> str:
    raw_ip = _http_request(PUBLIC_IP_URL).strip()

    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError as exc:
        raise RuntimeError(f"Invalid public IP returned by ipify: {raw_ip!r}") from exc

    if ip.version != 4:
        raise RuntimeError(f"Expected IPv4 address, got: {ip}")

    return str(ip)


def _get_headers() -> dict[str, str]:
    return {
        "X-API-Key": _get_required_env("SPACESHIP_API_KEY"),
        "X-API-Secret": _get_required_env("SPACESHIP_API_SECRET"),
    }


def _get_dns_records(
    headers: Mapping[str, str], domain: str
) -> list[dict[str, object]]:
    url = f"{SPACESHIP_BASE_URL}/{domain}?take=500&skip=0"

    raw_response = _http_request(
        url=url,
        headers=headers,
    )

    try:
        response: object = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Spaceship returned invalid JSON") from exc

    if not isinstance(response, dict):
        raise TypeError("Spaceship response must be an object")

    items = response.get("items")

    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise TypeError("Spaceship response does not contain an items list")

    return cast("list[dict[str, object]]", items)


def _add_a_record(
    headers: Mapping[str, str],
    domain: str,
    record_name: str,
    address: str,
    ttl: int,
) -> None:
    url = f"{SPACESHIP_BASE_URL}/{domain}"

    payload = {
        "force": True,
        "items": [
            {
                "type": "A",
                "name": record_name,
                "address": address,
                "ttl": ttl,
            }
        ],
    }

    _http_request(
        url=url,
        method="PUT",
        headers=headers,
        data=payload,
    )


def _delete_a_records(
    headers: Mapping[str, str],
    domain: str,
    record_name: str,
    addresses: list[str],
) -> None:
    if not addresses:
        return

    url = f"{SPACESHIP_BASE_URL}/{domain}"

    payload = [
        {
            "type": "A",
            "name": record_name,
            "address": address,
        }
        for address in addresses
    ]

    _http_request(
        url=url,
        method="DELETE",
        headers=headers,
        data=payload,
    )


def main() -> int:
    domain = _get_required_env("DOMAIN")
    record_name = os.getenv("RECORD_NAME", "@")

    try:
        ttl = int(os.getenv("TTL", "300"))
    except ValueError as exc:
        raise RuntimeError("TTL must be an integer") from exc

    if ttl <= 0:
        raise RuntimeError("TTL must be greater than zero")

    headers = _get_headers()
    public_ip = _get_public_ip()

    logger.info("Current public IP: %s", public_ip)

    records = _get_dns_records(headers, domain)

    a_records = [
        record
        for record in records
        if record.get("type") == "A" and record.get("name") == record_name
    ]

    addresses = [
        address
        for record in a_records
        if isinstance(address := record.get("address"), str) and address
    ]

    logger.info(
        "Current Spaceship A records for %s: %s",
        domain,
        ", ".join(addresses) if addresses else "none",
    )

    current_exists = public_ip in addresses

    if not current_exists:
        logger.info(
            "Adding A record %s (%s) -> %s",
            record_name,
            domain,
            public_ip,
        )

        _add_a_record(
            headers,
            domain,
            record_name,
            public_ip,
            ttl,
        )

        logger.info("Added current IP successfully")

    obsolete_addresses = [address for address in addresses if address != public_ip]

    if obsolete_addresses:
        logger.info(
            "Removing obsolete IPs: %s",
            ", ".join(obsolete_addresses),
        )
        _delete_a_records(
            headers,
            domain,
            record_name,
            obsolete_addresses,
        )
        logger.info("Removed obsolete IPs successfully")

    if current_exists and not obsolete_addresses:
        logger.info("DNS already up to date")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, TypeError, OSError) as exc:
        logger.error("%s", exc)
        sys.exit(1)
