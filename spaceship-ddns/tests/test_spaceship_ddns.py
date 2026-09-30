import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "spaceship-ddns.py"
SPEC = importlib.util.spec_from_file_location("spaceship_ddns", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ddns = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ddns)


class DdnsTests(unittest.TestCase):
    def run_updater(self, records, *, public_ip="192.0.2.10", fail_save=False):
        calls = []

        def request(url, method="GET", headers=None, data=None):
            if url == ddns.PUBLIC_IP_URL:
                return public_ip
            if method == "GET":
                return json.dumps({"items": records})
            calls.append((method, data))
            if fail_save and method == "PUT":
                raise RuntimeError("Save failed")
            return ""

        env = {
            "DOMAIN": "example.net",
            "RECORD_NAME": "home",
            "TTL": "300",
            "SPACESHIP_API_KEY": "test-key",
            "SPACESHIP_API_SECRET": "test-secret",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(ddns, "_http_request", side_effect=request),
            self.assertLogs(ddns.logger, level="INFO") as logs,
        ):
            if fail_save:
                with self.assertRaisesRegex(RuntimeError, "Save failed"):
                    ddns.main()
            else:
                self.assertEqual(ddns.main(), 0)
        return calls, "\n".join(logs.output)

    def test_same_ip_changed_ttl_updates_only_target(self):
        records = [
            {"type": "A", "name": "home", "address": "192.0.2.10", "ttl": 3600},
            {"type": "A", "name": "other", "address": "192.0.2.20", "ttl": 3600},
            {"type": "AAAA", "name": "home", "address": "2001:db8::1", "ttl": 3600},
        ]
        calls, logs = self.run_updater(records)
        self.assertEqual(
            calls,
            [
                (
                    "PUT",
                    {
                        "force": True,
                        "items": [
                            {
                                "type": "A",
                                "name": "home",
                                "address": "192.0.2.10",
                                "ttl": 300,
                            }
                        ],
                    },
                )
            ],
        )
        self.assertNotIn("DNS already up to date", logs)

    def test_same_ip_same_ttl_does_not_write(self):
        records = [{"type": "A", "name": "home", "address": "192.0.2.10", "ttl": 300}]
        calls, logs = self.run_updater(records)
        self.assertEqual(calls, [])
        self.assertIn("DNS already up to date", logs)

    def test_ip_change_saves_before_deleting_obsolete_address(self):
        records = [
            {"type": "A", "name": "home", "address": "192.0.2.11", "ttl": 300},
            {"type": "A", "name": "other", "address": "192.0.2.20", "ttl": 300},
        ]
        calls, _ = self.run_updater(records)
        self.assertEqual([method for method, _ in calls], ["PUT", "DELETE"])
        self.assertEqual(calls[0][1]["items"][0]["address"], "192.0.2.10")
        self.assertEqual(
            calls[1][1],
            [{"type": "A", "name": "home", "address": "192.0.2.11"}],
        )

    def test_existing_ip_removes_only_obsolete_target_address(self):
        records = [
            {"type": "A", "name": "home", "address": "192.0.2.10", "ttl": 300},
            {"type": "A", "name": "home", "address": "192.0.2.11", "ttl": 300},
            {"type": "A", "name": "other", "address": "192.0.2.20", "ttl": 300},
        ]
        calls, _ = self.run_updater(records)
        self.assertEqual(
            calls,
            [
                (
                    "DELETE",
                    [{"type": "A", "name": "home", "address": "192.0.2.11"}],
                )
            ],
        )

    def test_failed_ttl_update_does_not_report_success_or_delete(self):
        records = [
            {"type": "A", "name": "home", "address": "192.0.2.10", "ttl": 3600},
            {"type": "A", "name": "home", "address": "192.0.2.11", "ttl": 3600},
        ]
        calls, logs = self.run_updater(records, fail_save=True)
        self.assertEqual([method for method, _ in calls], ["PUT"])
        self.assertNotIn("DNS already up to date", logs)
        self.assertNotIn("Saved current A record successfully", logs)


if __name__ == "__main__":
    unittest.main()
