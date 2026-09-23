import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import router
from mikrotik_reporting.config import load_common_config, load_routeros_config
from mikrotik_reporting.routeros import fetch_snapshot, uptime_seconds


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
                os.environ["MIKROTIK_REST_URL"] = "http://router/rest"
                with self.assertRaisesRegex(ValueError, "HTTPS URL"):
                    load_routeros_config()


if __name__ == "__main__":
    unittest.main()
