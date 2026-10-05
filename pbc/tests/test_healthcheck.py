"""Exercise optional health pings with fake network and backup commands."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "pbc_backup_data.py"


class HealthcheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        binaries = self.base / "bin"
        binaries.mkdir()
        for name, body in {
            "curl": (
                "import json, os, sys\n"
                "with open(os.environ['EVENTS'], 'a') as stream:\n"
                "    stream.write(json.dumps(['curl', sys.argv[1:]]) + '\\n')\n"
                "sys.exit(int(os.environ.get('CURL_STATUS', '0')))\n"
            ),
            "proxmox-backup-client": (
                "import json, os, sys\n"
                "with open(os.environ['EVENTS'], 'a') as stream:\n"
                "    stream.write(json.dumps(['backup', sys.argv[1:]]) + '\\n')\n"
                "sys.exit(int(os.environ.get('BACKUP_STATUS', '0')))\n"
            ),
        }.items():
            path = binaries / name
            path.write_text("#!/usr/bin/env python3\n" + body)
            path.chmod(0o755)
        mail = binaries / "msmtp"
        mail.write_text("#!/usr/bin/env bash\ncat >/dev/null\n")
        mail.chmod(0o755)
        source = self.base / "source"
        source.mkdir()
        self.events = self.base / "events.jsonl"
        self.log = self.base / "backup.log"
        self.environment = {
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "EVENTS": str(self.events),
            "LOGFILE": str(self.log),
            "SOURCE_DIR": str(source),
            "REPO": "fake-repository",
            "BACKUP_NAME": "data.pxar",
            "RECIPIENT_EMAIL": "to@example.com",
            "SENDER_EMAIL": "from@example.com",
            "MSMTP_ACCOUNT": "default",
        }

    def run_backup(self, **settings):
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=self.environment | settings,
            capture_output=True,
            text=True,
            check=False,
        )

    def read_events(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_unset_url_does_not_ping(self):
        self.assertEqual(self.run_backup().returncode, 0)
        self.assertEqual([event[0] for event in self.read_events()], ["backup"])

    def test_start_and_success_pings_surround_backup(self):
        self.assertEqual(
            self.run_backup(HEALTHCHECK_URL="https://example.test/private/").returncode,
            0,
        )
        events = self.read_events()
        self.assertEqual([event[0] for event in events], ["curl", "backup", "curl"])
        self.assertEqual(events[0][1][-1], "https://example.test/private/start")
        self.assertEqual(events[2][1][-1], "https://example.test/private")

    def test_failure_ping_and_monitoring_outage_preserve_backup_status(self):
        url = "https://example.test/private"
        result = self.run_backup(
            HEALTHCHECK_URL=url, BACKUP_STATUS="23", CURL_STATUS="7"
        )
        self.assertEqual(result.returncode, 23)
        events = self.read_events()
        self.assertEqual(events[0][1][-1], url + "/start")
        self.assertEqual(events[2][1][-1], url + "/fail")
        self.assertEqual(self.log.read_text().count("Warning: Healthcheck"), 2)
        self.assertNotIn(url, self.log.read_text())

    def test_monitoring_outage_does_not_fail_successful_backup(self):
        result = self.run_backup(
            HEALTHCHECK_URL="https://example.test/private", CURL_STATUS="28"
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.log.read_text().count("Warning: Healthcheck"), 2)

    def test_failed_pre_hook_sends_only_failure_ping(self):
        hook = self.base / "pre.sh"
        hook.write_text("#!/bin/sh\nexit 12\n")
        hook.chmod(0o755)
        url = "https://example.test/private"
        result = self.run_backup(PRE_BACKUP_HOOK=str(hook), HEALTHCHECK_URL=url)
        self.assertEqual(result.returncode, 12)
        events = self.read_events()
        self.assertEqual([event[0] for event in events], ["curl"])
        self.assertEqual(events[0][1][-1], url + "/fail")

    def test_configuration_error_sends_failure_ping_without_start(self):
        url = "https://example.test/private"
        result = self.run_backup(BACKUP_ID="", HEALTHCHECK_URL=url)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BACKUP_ID", result.stderr)
        events = self.read_events()
        self.assertEqual([event[0] for event in events], ["curl"])
        self.assertEqual(events[0][1][-1], url + "/fail")
        self.assertIn("Configuration error: BACKUP_ID", self.log.read_text())

    def test_failed_post_hook_changes_final_ping_to_failure(self):
        hook = self.base / "post.sh"
        hook.write_text("#!/bin/sh\nexit 17\n")
        hook.chmod(0o755)
        url = "https://example.test/private"
        result = self.run_backup(POST_BACKUP_HOOK=str(hook), HEALTHCHECK_URL=url)
        self.assertEqual(result.returncode, 17)
        events = self.read_events()
        self.assertEqual([event[0] for event in events], ["curl", "backup", "curl"])
        self.assertEqual(events[0][1][-1], url + "/start")
        self.assertEqual(events[2][1][-1], url + "/fail")


if __name__ == "__main__":
    unittest.main()
