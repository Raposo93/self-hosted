import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from helpers import UTC, router
from mikrotik_reporting.config import load_common_config, load_routeros_config
from mikrotik_reporting.routeros import (
    fetch_detection_batch,
    fetch_snapshot,
    log_datetime,
    uptime_seconds,
)


class RouterOSTests(unittest.TestCase):
    def test_fetch_snapshot_selects_only_matching_drop_rules(self) -> None:
        config = router()
        calls: list[tuple[str, dict[str, str]]] = []

        def fake_get(_config, path: str, params: dict[str, str]) -> object:
            calls.append((path, params))
            if path == "ip/firewall/raw":
                return [
                    {
                        ".id": "*1",
                        "comment": "Drop WAN scanners",
                        "action": "drop",
                        "src-address-list": "wan-scanners",
                        "packets": "12",
                        "bytes": "1200",
                    },
                    {
                        ".id": "*3",
                        "comment": "unrelated",
                        "action": "drop",
                        "src-address-list": "other",
                        "packets": "900",
                        "bytes": "90000",
                    },
                ]
            if path == "ip/firewall/filter":
                return [
                    {
                        ".id": "*2",
                        "comment": "crowdsec-bouncer:filter-input @cs-routeros-bouncer",
                        "action": "drop",
                        "src-address-list": "crowdsec-banned",
                        "packets": "5",
                        "bytes": "500",
                    }
                ]
            if path == "ip/firewall/address-list":
                return (
                    [{".id": "*A"}, {".id": "*B"}]
                    if params["list"] == "wan-scanners"
                    else [{".id": "*C"}]
                )
            if path == "system/resource":
                return {"uptime": "1w2d03:04:05"}
            raise AssertionError(path)

        with patch("mikrotik_reporting.routeros._get_json", side_effect=fake_get):
            snapshot = fetch_snapshot(config)
        self.assertEqual(
            snapshot["counters"],
            {
                "local:raw:*1": {"packets": 12, "bytes": 1200},
                "crowdsec:filter:*2": {"packets": 5, "bytes": 500},
            },
        )
        self.assertEqual(snapshot["sizes"], {"local": 2, "crowdsec": 1})
        self.assertEqual(snapshot["uptime"], 9 * 86400 + 3 * 3600 + 4 * 60 + 5)
        self.assertEqual(len(calls), 5)

    def test_missing_local_rule_fails(self) -> None:
        with (
            patch("mikrotik_reporting.routeros._get_json", return_value=[]),
            self.assertRaisesRegex(ValueError, "exactly one local drop rule"),
        ):
            fetch_snapshot(router())

    def test_detection_log_is_parsed_and_non_port_events_are_skipped(self) -> None:
        rows = [
            {
                ".id": "*1",
                "time": "10:00:00",
                "topics": "firewall,info",
                "message": (
                    "mikrotik-report-detect input: in:ether1, proto TCP (SYN), "
                    "192.0.2.10:45678->198.51.100.1:22, len 60"
                ),
            },
            {
                ".id": "*2",
                "time": "sep/22 23:59:00",
                "topics": "firewall,info",
                "message": (
                    "mikrotik-report-detect input: in:ether1, proto UDP, "
                    "192.0.2.20:50000->198.51.100.1:6881, len 80"
                ),
            },
            {
                ".id": "*3",
                "time": "10:01:00",
                "topics": "firewall,info",
                "message": "mikrotik-report-detect input: proto ICMP, 192.0.2.30->198.51.100.1",
            },
            {
                ".id": "*4",
                "time": "10:02:00",
                "topics": "firewall,info",
                "message": "unrelated firewall message",
            },
        ]
        with patch("mikrotik_reporting.routeros._get_json", return_value=rows) as get:
            batch = fetch_detection_batch(
                router(), datetime(2026, 9, 23, 12, tzinfo=timezone.utc), UTC
            )
        self.assertEqual(len(batch["fingerprints"]), 3)
        self.assertEqual(
            [
                (
                    event["day"],
                    event["source_ip"],
                    event["protocol"],
                    event["destination_port"],
                )
                for event in batch["events"]
            ],
            [
                ("2026-09-23", "192.0.2.10", "tcp", 22),
                ("2026-09-22", "192.0.2.20", "udp", 6881),
            ],
        )
        self.assertEqual(get.call_args.args[1], "log")
        self.assertEqual(get.call_args.args[2]["buffer"], "mikrotik-report")

    def test_log_time_infers_previous_year_at_year_boundary(self) -> None:
        parsed = log_datetime(
            "dec/31 23:59:00",
            datetime(2027, 1, 1, 0, 5, tzinfo=timezone.utc),
            UTC,
        )
        self.assertEqual(parsed.isoformat(), "2026-12-31T23:59:00+00:00")

    def test_malformed_port_detection_log_fails_collection(self) -> None:
        rows = [
            {
                ".id": "*1",
                "time": "10:00:00",
                "topics": "firewall,info",
                "message": "mikrotik-report-detect input: proto TCP (SYN), malformed",
            }
        ]
        with (
            patch("mikrotik_reporting.routeros._get_json", return_value=rows),
            self.assertRaisesRegex(ValueError, "detection log message"),
        ):
            fetch_detection_batch(
                router(), datetime(2026, 9, 23, 12, tzinfo=timezone.utc), UTC
            )

    def test_uptime_accepts_both_routeros_formats(self) -> None:
        self.assertEqual(uptime_seconds("2d3h4m5s"), 2 * 86400 + 3 * 3600 + 245)
        self.assertEqual(uptime_seconds("1w2d03:04:05"), 9 * 86400 + 11045)
        for invalid in ("", "1d-junk", "1:60:00"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                uptime_seconds(invalid)

    def test_configuration_is_command_specific_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "report.sqlite3")
            environment = {
                "MIKROTIK_REPORT_DB": database,
                "MIKROTIK_REST_URL": "https://router.example.net/rest",
                "MIKROTIK_USER": "reader",
                "MIKROTIK_PASSWORD": "secret",
                "MIKROTIK_LOCAL_LIST": "local",
                "MIKROTIK_LOCAL_RULE_COMMENT": "drop local",
                "MIKROTIK_CROWDSEC_LIST": "crowdsec",
                "MIKROTIK_CROWDSEC_RULE_SIGNATURE": "@crowdsec",
            }
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual(load_common_config().state, Path(database))
                self.assertEqual(load_routeros_config().local_table, "raw")
                self.assertEqual(
                    load_routeros_config().detection_log_buffer, "mikrotik-report"
                )
                os.environ["MIKROTIK_REST_URL"] = "http://router/rest"
                with self.assertRaisesRegex(ValueError, "HTTPS URL"):
                    load_routeros_config()


if __name__ == "__main__":
    unittest.main()
