"""Regression checks for the guarded trial's configuration and cleanup boundaries."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from tests.test_nbfc_guarded_trial import create_synthetic_ec_file, create_synthetic_sysfs_sensors
from tools.nbfc_guarded_trial import DEFAULT_POLICY_PATH, ECGuardBinding, GuardedTrialSupervisor


class TestGuardReview(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ec = self.root / "fake_ec.bin"
        create_synthetic_ec_file(self.ec)
        self.descriptor = os.open(self.ec, os.O_RDWR)
        self.guard = ECGuardBinding()
        self.log = self.root / "audit.jsonl"

    def tearDown(self):
        self.guard.cleanup()
        os.close(self.descriptor)
        self.temporary.cleanup()

    def test_malformed_policy_cannot_leave_an_active_guard(self):
        original = DEFAULT_POLICY_PATH.read_text()
        cases = [
            original[:-2], original + "garbage",
            original.replace('"version": 1', '"version": 1, "version": 1'),
            original.replace('"allowed_values": [100]', '"allowed_values": [100,]'),
            original.replace('"0x03": 33', '"0x03": 33, "0x03": 33'),
            original + "\0", original[:100], "{}", original.replace('"version": 1', '"version": 01'),
        ]
        for index, content in enumerate(cases):
            with self.subTest(index=index):
                policy = self.root / "bad.json"
                policy.write_text(content)
                before = self.ec.read_bytes()
                self.assertNotEqual(self.guard.init(str(policy), str(self.log)), 0)
                self.assertNotEqual(self.guard.write_word(self.descriptor, 0x37, 100, "test"), 0)
                self.assertEqual(before, self.ec.read_bytes())

    def test_context_does_not_grant_restore_privileges(self):
        self.assertEqual(self.guard.init(str(DEFAULT_POLICY_PATH), str(self.log)), 0)
        self.assertNotEqual(self.guard.write_word(self.descriptor, 0x37, 0, "restore_snapshot"), 0)
        self.assertEqual(self.ec.read_bytes()[0x37], 0)

    def test_restore_failure_does_not_zero_a_fan_still_in_manual_mode(self):
        self.assertEqual(self.guard.init(str(DEFAULT_POLICY_PATH), str(self.log)), 0)
        self.assertEqual(self.guard.write_byte(self.descriptor, 0x22, 12, "mode"), 0)
        self.assertEqual(self.guard.write_word(self.descriptor, 0x37, 100, "speed"), 0)
        read_only = os.open(self.ec, os.O_RDONLY)
        try:
            self.assertNotEqual(self.guard.restore_snapshot(read_only), 0)
        finally:
            os.close(read_only)
        self.assertEqual(self.ec.read_bytes()[0x22], 12)
        self.assertEqual(self.ec.read_bytes()[0x37], 100)

    def test_missing_manifest_fails_attestation(self):
        supervisor = GuardedTrialSupervisor(synthetic_root=self.root,
                                            manifest_path=self.root / "missing.json")
        with self.assertRaises(FileNotFoundError):
            supervisor._attest_build()
        self.assertIsNone(supervisor.guard)

    def test_preflight_failure_preserves_existing_state(self):
        state = self.root / "state.json"
        original = b'{"TargetFanSpeeds":[25,25]}'
        state.write_bytes(original)
        supervisor = GuardedTrialSupervisor(synthetic_root=self.root,
                                            profile_path=self.root / "missing.json")
        self.assertEqual(supervisor.run(), 1)
        self.assertEqual(state.read_bytes(), original)

    def test_synthetic_paths_cannot_escape_root(self):
        create_synthetic_sysfs_sensors(self.root / "sys")
        with tempfile.TemporaryDirectory() as outside:
            external_ec = Path(outside) / "fake_ec.bin"
            create_synthetic_ec_file(external_ec)
            supervisor = GuardedTrialSupervisor(synthetic_root=self.root, ec_path=str(external_ec))
            with self.assertRaises(PermissionError):
                supervisor.preflight_checks()

    def test_missing_package_label_is_not_replaced_by_a_core_temperature(self):
        create_synthetic_sysfs_sensors(self.root / "sys")
        (self.root / "sys/class/hwmon/hwmon0/temp1_label").write_text("Core 0")
        supervisor = GuardedTrialSupervisor(synthetic_root=self.root)
        self.assertIsNone(supervisor.read_cpu_package_temp())


if __name__ == "__main__":
    unittest.main()
