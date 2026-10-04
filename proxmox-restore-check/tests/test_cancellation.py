"""Exercise cancellation with real signals and simulated PVE commands."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "proxmox_restore_check.py"

FAKE_CLI = """#!/usr/bin/env python3
import os
import signal
import sys
import time
from pathlib import Path

events = Path(os.environ['EVENTS'])
config = Path(os.environ['GUEST_CONFIG'])
phase = os.environ['PHASE']
name = Path(sys.argv[0]).name
args = sys.argv[1:]

def record(value):
    with events.open('a') as output:
        output.write(value + '\\n')

def wait_for_cancel():
    def stopped(_signal, _frame):
        time.sleep(0.2)
        record('worker-exit')
        sys.exit(143)
    signal.signal(signal.SIGTERM, stopped)
    record('worker-start')
    while True:
        time.sleep(0.05)

if name == 'pvesh':
    record('lookup')
    print('[{"volid":"pbs:backup/vm/100/2026-01-01T00:00:00Z"}]')
elif name == 'qmrestore':
    config.write_text(
        ('scsi0: foreign:vm-999-disk-0\\n' if os.environ.get('FOREIGN') else
         'scsi0: local-lvm:vm-999-disk-0\\n')
        + 'net0: bridge=vmbr0\\n'
    )
    if phase == 'restore':
        wait_for_cancel()
elif name == 'qm':
    if args[0] == 'start':
        record('start')
    elif args[0] == 'status':
        record('status')
        print('status: stopped' if phase == 'restore' else 'status: running')
    elif args[0] == 'agent':
        wait_for_cancel()
    elif args[0] == 'stop':
        record('stop')
    elif args[0] == 'destroy':
        record('destroy')
        config.unlink()
elif name == 'msmtp':
    Path(os.environ['MAIL']).write_text(sys.stdin.read())
    record('mail')
elif name == 'systemctl':
    record('poweroff')
"""

DRIVER = """
import importlib.util
import os
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location('restore_check', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

class TestRunner(module.Runner):
    def __init__(self, config, cancellation=None):
        super().__init__(config, cancellation)
        self.base = Path(os.environ['FAKE_BASE'])

    def preflight(self):
        pass

module.Runner = TestRunner
config = module.Config(
    'dr', 'pbs', 'local-lvm', 999, 'operator@example.com',
    [module.Guest('vm', 100, 'first', True),
     module.Guest('vm', 101, 'second', True)],
    poweroff=True,
)
raise SystemExit(module.run(config, False))
"""


class CancellationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        binaries = self.base / "bin"
        binaries.mkdir()
        for name in ("pvesh", "qmrestore", "qm", "msmtp", "systemctl"):
            path = binaries / name
            path.write_text(FAKE_CLI)
            path.chmod(0o755)
        (self.base / "qemu-server").mkdir()
        (self.base / "lxc").mkdir()
        self.config = self.base / "qemu-server" / "999.conf"
        self.events = self.base / "events"
        self.mail = self.base / "mail"
        self.environment = os.environ | {
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "FAKE_BASE": str(self.base),
            "GUEST_CONFIG": str(self.config),
            "EVENTS": str(self.events),
            "MAIL": str(self.mail),
        }

    def cancel_at(self, phase, *, foreign=False, cancellation_signal=signal.SIGTERM):
        process = subprocess.Popen(
            [sys.executable, "-c", DRIVER, str(MODULE)],
            env=self.environment | {"PHASE": phase, "FOREIGN": "1" if foreign else ""},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.events.exists() and "worker-start" in self.events.read_text():
                    break
                if process.poll() is not None:
                    self.fail("Restore check exited before cancellation")
                time.sleep(0.01)
            else:
                self.fail("Simulated PVE worker did not start")
            os.kill(process.pid, cancellation_signal)
            stdout, stderr = process.communicate(timeout=10)
            return (
                process.returncode,
                stdout,
                stderr,
                self.events.read_text().splitlines(),
            )
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def test_cancel_during_restore_waits_before_destroy(self):
        status, report, stderr, events = self.cancel_at("restore")
        self.assertEqual(status, 1, stderr)
        self.assertLess(events.index("worker-exit"), events.index("destroy"))
        self.assertEqual(events.count("lookup"), 1)
        self.assertNotIn("start", events)
        self.assertNotIn("poweroff", events)
        self.assertFalse(self.config.exists())
        self.assertIn("Cancelled: SIGTERM", report)
        self.assertIn("Cleanup: OK", report)
        self.assertIn("SKIP=1", self.mail.read_text())

    def test_cancel_after_start_stops_then_destroys(self):
        status, report, stderr, events = self.cancel_at("boot")
        self.assertEqual(status, 1, stderr)
        self.assertLess(events.index("start"), events.index("worker-exit"))
        self.assertLess(events.index("worker-exit"), events.index("stop"))
        self.assertLess(events.index("stop"), events.index("destroy"))
        self.assertEqual(events.count("lookup"), 1)
        self.assertNotIn("poweroff", events)
        self.assertFalse(self.config.exists())
        self.assertIn("Cancelled: SIGTERM", report)
        self.assertIn("Cleanup: OK", self.mail.read_text())

    def test_cancel_keeps_foreign_disk_for_manual_cleanup(self):
        status, report, stderr, events = self.cancel_at("restore", foreign=True)
        self.assertEqual(status, 1, stderr)
        self.assertIn("worker-exit", events)
        self.assertNotIn("destroy", events)
        self.assertTrue(self.config.exists())
        self.assertIn("Foreign disk reference", report)
        self.assertIn("manual intervention required", self.mail.read_text())
        self.assertNotIn("poweroff", events)

    def test_interactive_cancel_also_cleans_up(self):
        status, report, stderr, events = self.cancel_at(
            "boot", cancellation_signal=signal.SIGINT
        )
        self.assertEqual(status, 1, stderr)
        self.assertLess(events.index("worker-exit"), events.index("destroy"))
        self.assertIn("Cancelled: SIGINT", report)
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
