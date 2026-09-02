#!/usr/bin/env python3

import ipaddress
import json
import logging
import os
import sys
import urllib.error
import urllib.request


PUBLIC_IP_URL = "https://api.ipify.org"
SPACESHIP_BASE_URL = "https://spaceship.dev/api/v1/dns/records"
TIMEOUT = 15


logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger("spaceship-ddns")


def get_required_env(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")

    return value


def http_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    data: object | None = None,
) -> str:
    request_headers = headers.copy() if headers else {}
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


def get_public_ip() -> str:
    raw_ip = http_request(PUBLIC_IP_URL).strip()

    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid public IP returned by ipify: {raw_ip!r}"
        ) from exc

    if ip.version != 4:
        raise RuntimeError(f"Expected IPv4 address, got: {ip}")

    return str(ip)


def get_headers() -> dict:
    return {
        "X-API-Key": get_required_env("SPACESHIP_API_KEY"),
        "X-API-Secret": get_required_env("SPACESHIP_API_SECRET"),
    }


def get_dns_records(headers: dict, domain: str) -> list[dict]:
    url = f"{SPACESHIP_BASE_URL}/{domain}?take=500&skip=0"

    raw_response = http_request(
        url=url,
        headers=headers,
    )

    try:
        response = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Spaceship returned invalid JSON") from exc

    items = response.get("items")

    if not isinstance(items, list):
        raise RuntimeError(
            "Spaceship response does not contain an items list"
        )

    return items


def add_a_record(
    headers: dict,
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

    http_request(
        url=url,
        method="PUT",
        headers=headers,
        data=payload,
    )


def delete_a_records(
    headers: dict,
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

    http_request(
        url=url,
        method="DELETE",
        headers=headers,
        data=payload,
    )


def main() -> int:
    domain = get_required_env("DOMAIN")
    record_name = os.getenv("RECORD_NAME", "@")

    try:
        ttl = int(os.getenv("TTL", "300"))
    except ValueError as exc:
        raise RuntimeError("TTL must be an integer") from exc

    if ttl <= 0:
        raise RuntimeError("TTL must be greater than zero")

    headers = get_headers()
    public_ip = get_public_ip()

    logger.info("Current public IP: %s", public_ip)

    records = get_dns_records(headers, domain)

    a_records = [
        record
        for record in records
        if record.get("type") == "A"
        and record.get("name") == record_name
    ]

    addresses = [
        record.get("address")
        for record in a_records
        if record.get("address")
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

        add_a_record(
            headers,
            domain,
            record_name,
            public_ip,
            ttl,
        )

        logger.info("Added current IP successfully")

    obsolete_addresses = [
        address
        for address in addresses
        if address != public_ip
    ]

    if obsolete_addresses:
        logger.info(
            "Removing obsolete IPs: %s",
            ", ".join(obsolete_addresses),
        )
        delete_a_records(
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
    except Exception as exc:
        logger.error("%s", exc)
        sys.exit(1)