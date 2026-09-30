"""Exercise SSH config updates with real ssh -G and no network connections."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "setup-ssh-host.sh"


class SetupSSHHostTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.home = self.root / "home"
        self.ssh_dir = self.home / ".ssh"
        self.ssh_dir.mkdir(parents=True)
        self.config = self.ssh_dir / "config"
        (self.ssh_dir / "id_ed25519_example").write_text("fake private key\n")
        (self.ssh_dir / "id_ed25519_example.pub").write_text("fake public key\n")

        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        fake_copy = self.bin_dir / "ssh-copy-id"
        fake_copy.write_text("#!/bin/sh\nexit 0\n")
        fake_copy.chmod(0o755)

        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = f"{self.bin_dir}:{self.env['PATH']}"

    def run_setup(self, host="192.0.2.10", user="admin"):
        return subprocess.run(
            ["bash", str(SCRIPT), "example", user, host],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_previous_wildcard_user_conflict_preserves_config(self):
        original = "Host *\n    User legacy-user\n"
        self.config.write_text(original)

        result = self.run_setup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("effective SSH settings conflict", result.stderr)
        self.assertEqual(self.config.read_text(), original)

    def test_include_conflict_preserves_config(self):
        (self.ssh_dir / "extra.conf").write_text(
            "Host example\n    HostName 198.51.100.4\n"
        )
        original = "Include extra.conf\n"
        self.config.write_text(original)

        result = self.run_setup()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_text(), original)

    def test_unmanaged_alias_preserves_config(self):
        original = "Host example other\n    User admin\n"
        self.config.write_text(original)

        result = self.run_setup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unmanaged Host", result.stderr)
        self.assertEqual(self.config.read_text(), original)

    def test_invalid_destination_preserves_config(self):
        original = "Host other\n    User someone\n"
        self.config.write_text(original)

        result = self.run_setup(host='bad"host')

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_text(), original)

    def test_invalid_existing_config_preserves_config(self):
        original = 'Host other\n    User "unclosed\n'
        self.config.write_text(original)

        result = self.run_setup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate SSH configuration is invalid", result.stderr)
        self.assertEqual(self.config.read_text(), original)

    def test_failed_key_copy_preserves_config(self):
        original = "Host other\n    User someone\n"
        self.config.write_text(original)
        (self.bin_dir / "ssh-copy-id").write_text("#!/bin/sh\nexit 42\n")

        result = self.run_setup()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_text(), original)

    def test_update_is_repeatable_and_keeps_mode_600(self):
        original = "Host other\n    User someone\n"
        self.config.write_text(original)
        self.config.chmod(0o644)

        first = self.run_setup()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

        second = self.run_setup(host="192.0.2.11")
        self.assertEqual(second.returncode, 0, second.stderr)
        content = self.config.read_text()
        self.assertEqual(
            content.count("# BEGIN self-hosted ssh-client-setup: example"), 1
        )
        self.assertIn("HostName 192.0.2.11", content)
        self.assertNotIn("192.0.2.10", content)
        self.assertIn(original, content)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

        third = self.run_setup(host="192.0.2.11")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(self.config.read_text(), content)

    def test_new_config_is_mode_600(self):
        result = self.run_setup()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
