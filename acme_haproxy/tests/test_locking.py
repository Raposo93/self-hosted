"""Check ACME operation locking across independent processes."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "acme_haproxy.py"

DRIVER = """
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location('acme_haproxy', sys.argv[1])
acme = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = acme
spec.loader.exec_module(acme)
root = Path(os.environ['FAKE_ROOT'])
role = os.environ['ROLE']
acme.PENDING_STATE = root / '.pending.json'
acme.HAPROXY_HOSTS_MAP = root / 'hosts.map'
acme.LOCK_TIMEOUT = float(os.environ.get('LOCK_TIMEOUT', '30'))

def record(event):
    with (root / 'events').open('a') as output:
        output.write(event + '\\n')

original_flock = acme.fcntl.flock
def flock(fd, flags):
    record('lock-attempt-' + role)
    return original_flock(fd, flags)
acme.fcntl.flock = flock

def paths(_home, _domain):
    return {
        'fullchain': root / 'fullchain.cer',
        'keyfile': root / 'domain.key',
        'cert_dest': root / 'domain.pem',
    }

original_load = acme.PendingState.__init__
def load(self):
    record('load-' + role)
    original_load(self)
acme.PendingState.__init__ = load

def renew(_acme_sh, _domain):
    record('renew-' + role)
    if role == 'A':
        record('holding-A')
        while not (root / 'release').exists():
            time.sleep(0.02)
        return True
    return False

def issue(_acme_sh, _domain):
    record('issue-' + role)

def reload():
    record('reload-' + role)
    if role in os.environ['FAIL_RELOAD'].split(','):
        raise subprocess.CalledProcessError(1, 'systemctl')

acme._get_certificate_paths = paths
acme._renew_certificate = renew
acme._issue_certificate = issue
acme._install_certificate = lambda *_args: None
acme._validate_haproxy_config = lambda: record('validate-' + role)
acme._reload_haproxy = reload
record('call-' + role)
raise SystemExit(acme.issue('example.com') if os.environ['ACTION'] == 'issue'
                 else acme.renew_all())
"""


class LockingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "hosts.map").write_text("example.com backend\n")
        (self.root / "fullchain.cer").write_bytes(b"new certificate\n")
        (self.root / "domain.key").write_bytes(b"new private key\n")
        (self.root / "domain.pem").write_bytes(b"old certificate\nold private key\n")
        self.events = self.root / "events"
        self.processes = []
        self.addCleanup(self.stop_processes)

    def start(self, role, action="renew", fail_reload="", lock_timeout="30"):
        process = subprocess.Popen(
            [sys.executable, "-c", DRIVER, str(SCRIPT)],
            env=os.environ
            | {
                "FAKE_ROOT": str(self.root),
                "LOCK_TIMEOUT": lock_timeout,
                "ROLE": role,
                "ACTION": action,
                "FAIL_RELOAD": fail_reload,
                "HOME": str(self.root),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.processes.append(process)
        return process

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def events_so_far(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def wait_for(self, event):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if event in self.events_so_far():
                return
            time.sleep(0.01)
        self.fail(f"Timed out waiting for {event}: {self.events_so_far()}")

    def result(self, process):
        stdout, stderr = process.communicate(timeout=5)
        self.assertFalse(stderr, stderr)
        return process.returncode, stdout

    def test_busy_lock_exits_within_limit(self):
        first = self.start("A")
        self.wait_for("holding-A")
        second = self.start("B", lock_timeout="0.2")
        code, output = self.result(second)
        self.assertEqual(code, 1)
        self.assertIn("retry later", output)
        self.assertNotIn("load-B", self.events_so_far())
        (self.root / "release").touch()
        self.assertEqual(self.result(first)[0], 0)

    def test_waiting_renew_reads_pending_reload_and_retries_it(self):
        first = self.start("A", fail_reload="A,B")
        self.wait_for("holding-A")
        second = self.start("B", fail_reload="A,B")
        self.wait_for("lock-attempt-B")
        time.sleep(0.1)
        self.assertNotIn("load-B", self.events_so_far())
        self.assertIsNone(second.poll())

        (self.root / "release").touch()
        self.assertEqual(self.result(first)[0], 1)
        self.assertEqual(self.result(second)[0], 1)
        events = self.events_so_far()
        self.assertLess(events.index("reload-A"), events.index("load-B"))
        self.assertIn("reload-B", events)
        self.assertNotIn("renew-B", events)
        self.assertEqual(
            json.loads((self.root / ".pending.json").read_text()),
            {"example.com": "reload"},
        )

        third = self.start("C")
        self.assertEqual(self.result(third)[0], 0)
        self.assertIn("reload-C", self.events_so_far())
        self.assertNotIn("renew-C", self.events_so_far())
        self.assertFalse((self.root / ".pending.json").exists())
        self.assertEqual(
            (self.root / ".acme-haproxy.lock").stat().st_mode & 0o777, 0o600
        )

    def test_issue_waits_for_renew_under_the_same_lock(self):
        first = self.start("A")
        self.wait_for("holding-A")
        second = self.start("B", action="issue")
        self.wait_for("lock-attempt-B")
        time.sleep(0.1)
        self.assertNotIn("load-B", self.events_so_far())
        self.assertNotIn("issue-B", self.events_so_far())

        (self.root / "release").touch()
        self.assertEqual(self.result(first)[0], 0)
        self.assertEqual(self.result(second)[0], 0)
        events = self.events_so_far()
        self.assertLess(events.index("reload-A"), events.index("load-B"))
        self.assertIn("issue-B", events)
        self.assertFalse((self.root / ".pending.json").exists())


if __name__ == "__main__":
    unittest.main()
