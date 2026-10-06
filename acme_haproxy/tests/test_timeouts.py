"""Exercise real local process trees, without ACME or HAProxy access."""

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from test_locking import DRIVER, SCRIPT, LockingTests

HANG = """
import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(sys.argv[1]).write_text(str(child.pid))
while True:
    time.sleep(1)
"""


class TimeoutTests(unittest.TestCase):
    root: Path
    processes: list[subprocess.Popen[str]]

    setUp = LockingTests.setUp
    start = LockingTests.start
    stop_processes = LockingTests.stop_processes
    result = LockingTests.result

    def test_timeout_and_sigterm_stop_children_and_release_lock(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                pidfile = self.root / "child.pid"
                pidfile.unlink(missing_ok=True)
                driver = DRIVER.replace(
                    "acme._renew_certificate = renew",
                    "acme._renew_certificate = lambda *_: acme._run_command("
                    "[sys.executable, '-c', os.environ['HANG'], str(root / 'child.pid')], "
                    "timeout=0.5 if os.environ['CANCEL'] == 'False' else 60, check=True)",
                )
                process = subprocess.Popen(
                    [sys.executable, "-c", driver, str(SCRIPT)],
                    env=os.environ
                    | {
                        "FAKE_ROOT": str(self.root),
                        "ROLE": "T",
                        "ACTION": "renew",
                        "FAIL_RELOAD": "",
                        "HOME": str(self.root),
                        "HANG": HANG,
                        "CANCEL": str(cancel),
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                self.processes.append(process)
                deadline = time.monotonic() + 5
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(pidfile.exists())
                child = int(pidfile.read_text())
                if cancel:
                    process.send_signal(signal.SIGTERM)
                code, _ = self.result(process)
                self.assertEqual(code, 143 if cancel else 1)
                # Orphan zombies may await the host's reaper, but cannot do work.
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    status = Path(f"/proc/{child}/stat")
                    if not status.exists() or status.read_text().split()[2] == "Z":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("Child still active after cancellation")
                self.assertEqual(self.result(self.start("C"))[0], 0)


if __name__ == "__main__":
    unittest.main()
