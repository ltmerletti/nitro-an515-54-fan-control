"""Opt-in Linux tests running the compiled upstream child against a regular EC fixture.

Set NITROFAN_GUARDED_MANIFEST to the private build's manifest to run these.
No root privileges, module changes, or hardware EC files are used.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

from tests.test_nbfc_guarded_trial import create_synthetic_ec_file, create_synthetic_sysfs_sensors


MANIFEST = os.environ.get("NITROFAN_GUARDED_MANIFEST")


@unittest.skipUnless(MANIFEST and sys.platform == "linux", "Requires an opt-in private Linux build")
class TestPatchedUpstream(unittest.TestCase):
    def setUp(self):
        self.manifest_path = Path(MANIFEST)
        self.manifest = json.loads(self.manifest_path.read_text())
        self.root = Path(self.manifest["runtime_root"])
        self.ec = self.root / "fake_ec.bin"
        create_synthetic_ec_file(self.ec)
        self.baseline = self.ec.read_bytes()
        create_synthetic_sysfs_sensors(self.root / "sys")
        self.state = self.root / "var/lib/nbfc/state.json"
        self.original_state = b'{"TargetFanSpeeds":[25,25]}'
        self.state.write_bytes(self.original_state)
        self.audit = self.root / "ec_guard_audit.log"
        self.audit.unlink(missing_ok=True)
        self.command = [
            sys.executable, "-m", "tools.nbfc_guarded_trial",
            "--synthetic-root", str(self.root), "--verify-build",
            "--manifest", str(self.manifest_path),
        ]
        self.output = (self.root / (self._testMethodName + ".log")).open("w")
        self.supervisor = None

    def tearDown(self):
        if self.supervisor and self.supervisor.poll() is None:
            self.supervisor.send_signal(signal.SIGTERM)
            self.supervisor.wait(timeout=12)
        self.output.close()

    def start(self, duration=10):
        self.supervisor = subprocess.Popen(
            self.command + ["--duration", str(duration)], stdout=self.output,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    def await_maximum(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.supervisor.poll() is not None:
                self.fail("Supervisor exited before maximum writes; see " + self.output.name)
            content = self.ec.read_bytes()
            if content[0x37] == 100 and content[0x3a] == 100:
                return
            time.sleep(0.05)
        self.fail("Maximum targets were not written")

    def check_restoration(self):
        self.assertEqual(self.ec.read_bytes(), self.baseline)
        self.assertEqual(self.state.read_bytes(), self.original_state)
        records = [json.loads(line) for line in self.audit.read_text().splitlines()]
        active_speeds = [record for record in records if record["action"] == "ALLOWED"
                         and record["offset"] in ("0x37", "0x3A")]
        self.assertTrue(active_speeds)
        self.assertTrue(all(record["new"] == ["0x64", "0x00"] for record in active_speeds))
        self.assertFalse(any(record["offset"] == "0x03" for record in records))
        restored = [record["offset"] for record in records if record["action"] == "RESTORED"]
        self.assertLess(restored.index("0x22"), restored.index("0x37"))
        self.assertLess(restored.index("0x21"), restored.index("0x3A"))

    def test_bounded_maximum_restores_baseline_and_stale_state(self):
        self.start(duration=2)
        self.await_maximum()
        self.assertEqual(self.supervisor.wait(timeout=12), 0)
        self.check_restoration()

    def test_child_sigkill_restores_baseline_and_fails(self):
        self.start()
        self.await_maximum()
        process_id = self.supervisor.pid
        children = Path(f"/proc/{process_id}/task/{process_id}/children").read_text().split()
        self.assertEqual(len(children), 1)
        os.kill(int(children[0]), signal.SIGKILL)
        self.assertEqual(self.supervisor.wait(timeout=12), 1)
        self.check_restoration()

    def test_supervisor_sigterm_restores_baseline(self):
        self.start()
        self.await_maximum()
        self.supervisor.send_signal(signal.SIGTERM)
        self.assertEqual(self.supervisor.wait(timeout=12), 143)
        self.check_restoration()

    def test_sensor_loss_restores_baseline_and_fails(self):
        self.start()
        self.await_maximum()
        (self.root / "sys/class/hwmon/hwmon0/temp1_input").unlink()
        self.assertEqual(self.supervisor.wait(timeout=12), 1)
        self.check_restoration()

    def test_baseline_mismatch_performs_no_writes(self):
        content = bytearray(self.baseline)
        content[3] = 0
        self.ec.write_bytes(content)
        self.start(duration=2)
        self.assertEqual(self.supervisor.wait(timeout=12), 1)
        self.assertEqual(self.ec.read_bytes(), content)
        self.assertEqual(self.state.read_bytes(), self.original_state)
        self.assertEqual(self.audit.read_text(), "")
