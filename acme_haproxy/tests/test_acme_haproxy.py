import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "acme_haproxy.py"
SPEC = importlib.util.spec_from_file_location("acme_haproxy", SCRIPT)
assert SPEC and SPEC.loader
acme = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acme)


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.fullchain = self.root / "fullchain.cer"
        self.keyfile = self.root / "domain.key"
        self.pem = self.root / "domain.pem"
        self.fullchain.write_bytes(b"new certificate\n")
        self.keyfile.write_bytes(b"new private key\n")
        self.pem.write_bytes(b"old certificate\nold private key\n")
        self.map = self.root / "hosts.map"
        self.map.write_text("example.com backend\n")
        self.patchers = [
            mock.patch.object(acme, "PENDING_STATE", self.root / ".pending.json"),
            mock.patch.object(acme, "HAPROXY_HOSTS_MAP", self.map),
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch.object(acme, "_get_certificate_paths", side_effect=self.paths),
            mock.patch.object(acme, "_validate_haproxy_config"),
            mock.patch.object(acme, "_reload_haproxy"),
            mock.patch.object(acme, "_install_certificate"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def paths(self, _home, _domain):
        return {
            "fullchain": self.fullchain,
            "keyfile": self.keyfile,
            "cert_dest": self.pem,
        }

    def test_failed_install_is_retried_without_renewing_again(self):
        with mock.patch.object(acme, "_renew_certificate", return_value=True) as renew:
            with mock.patch.object(
                acme,
                "_install_certificate",
                side_effect=subprocess.CalledProcessError(1, "acme.sh"),
            ):
                self.assertEqual(acme.renew_all(), 1)
            self.assertEqual(acme.PendingState().phases, {"example.com": "deploy"})
            self.assertEqual(acme.renew_all(), 0)
            renew.assert_called_once()
        self.assertEqual(self.pem.read_bytes(), b"new certificate\nnew private key\n")
        self.assertEqual(acme.PendingState().phases, {})

    def test_repeated_deployment_failure_stays_pending(self):
        with (
            mock.patch.object(acme, "_renew_certificate", return_value=True) as renew,
            mock.patch.object(
                acme,
                "_install_certificate",
                side_effect=subprocess.CalledProcessError(1, "acme.sh"),
            ),
        ):
            self.assertEqual(acme.renew_all(), 1)
            self.assertEqual(acme.renew_all(), 1)
            renew.assert_called_once()
        self.assertEqual(acme.PendingState().phases, {"example.com": "deploy"})

    def test_failed_reload_is_retried_on_next_run(self):
        with mock.patch.object(acme, "_renew_certificate", return_value=True) as renew:
            with mock.patch.object(
                acme,
                "_reload_haproxy",
                side_effect=subprocess.CalledProcessError(1, "systemctl"),
            ):
                self.assertEqual(acme.renew_all(), 1)
            self.assertEqual(acme.PendingState().phases, {"example.com": "reload"})
            self.assertEqual(acme.renew_all(), 0)
            renew.assert_called_once()
        self.assertEqual(acme.PendingState().phases, {})
        self.assertFalse(acme._backup_path(self.pem).exists())

    def test_skipped_renewal_with_matching_pem_does_not_reload(self):
        self.pem.write_bytes(self.fullchain.read_bytes() + self.keyfile.read_bytes())
        with mock.patch.object(acme, "_renew_certificate", return_value=False):
            self.assertEqual(acme.renew_all(), 0)
        acme._reload_haproxy.assert_not_called()
        self.assertEqual(acme.PendingState().phases, {})

    def test_completed_renewal_before_state_update_is_reconciled(self):
        acme.PendingState().set("example.com", "renew")
        with mock.patch.object(acme, "_renew_certificate", return_value=False):
            self.assertEqual(acme.renew_all(), 0)
        self.assertEqual(self.pem.read_bytes(), b"new certificate\nnew private key\n")
        acme._reload_haproxy.assert_called_once()

    def test_validation_failure_restores_old_pem_and_retries(self):
        with mock.patch.object(acme, "_renew_certificate", return_value=True):
            with mock.patch.object(
                acme,
                "_validate_haproxy_config",
                side_effect=subprocess.CalledProcessError(1, "haproxy"),
            ):
                self.assertEqual(acme.renew_all(), 1)
            self.assertEqual(
                self.pem.read_bytes(), b"old certificate\nold private key\n"
            )
            self.assertEqual(acme.PendingState().phases, {"example.com": "deploy"})
            self.assertEqual(acme.renew_all(), 0)

    def test_failed_restore_keeps_pending_state(self):
        with (
            mock.patch.object(acme, "_renew_certificate", return_value=True),
            mock.patch.object(
                acme,
                "_validate_haproxy_config",
                side_effect=subprocess.CalledProcessError(1, "haproxy"),
            ),
            mock.patch.object(
                acme, "_restore_pem", side_effect=OSError("restore failed")
            ),
            self.assertLogs(acme.log, level="ERROR") as logs,
        ):
            self.assertEqual(acme.renew_all(), 1)
        self.assertIn("Failed to restore previous PEM", "\n".join(logs.output))
        self.assertEqual(acme.PendingState().phases, {"example.com": "deploy"})

    def test_validation_failure_removes_new_unvalidated_pem(self):
        self.pem.unlink()
        with (
            mock.patch.object(acme, "_renew_certificate", return_value=True),
            mock.patch.object(
                acme,
                "_validate_haproxy_config",
                side_effect=subprocess.CalledProcessError(1, "haproxy"),
            ),
        ):
            self.assertEqual(acme.renew_all(), 1)
        self.assertFalse(self.pem.exists())
        self.assertEqual(acme.PendingState().phases, {"example.com": "deploy"})

    def test_two_deployments_validate_and_reload_once(self):
        second_pem = self.root / "second.pem"
        second_pem.write_bytes(b"old second certificate\n")
        self.map.write_text("example.com backend\nsecond.example.com backend\n")

        def domain_paths(_home, domain):
            paths = self.paths(_home, domain)
            if domain == "second.example.com":
                paths["cert_dest"] = second_pem
            return paths

        with (
            mock.patch.object(acme, "_get_certificate_paths", side_effect=domain_paths),
            mock.patch.object(acme, "_renew_certificate", return_value=True),
        ):
            self.assertEqual(acme.renew_all(), 0)
        acme._validate_haproxy_config.assert_called_once()
        acme._reload_haproxy.assert_called_once()

    def test_pem_replacement_is_atomic_and_private(self):
        original_replace = acme.os.replace

        def inspect_replace(source, destination):
            if destination == self.pem:
                self.assertEqual(
                    self.pem.read_bytes(), b"old certificate\nold private key\n"
                )
                self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
            return original_replace(source, destination)

        with mock.patch.object(acme.os, "replace", side_effect=inspect_replace):
            acme._build_haproxy_pem(self.fullchain, self.keyfile, self.pem)
        self.assertEqual(self.pem.stat().st_mode & 0o777, 0o600)
        self.assertEqual(acme._backup_path(self.pem).stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
