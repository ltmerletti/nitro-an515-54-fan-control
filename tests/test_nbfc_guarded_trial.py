"""Unit tests for tools/nbfc_guarded_trial.py, C guard, and supervised trial lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tools.nbfc_guarded_trial import (
    DEFAULT_POLICY_PATH,
    DEFAULT_PROFILE_PATH,
    ECGuardBinding,
    GuardedTrialSupervisor,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def create_synthetic_ec_file(path: Path) -> None:
    """Create a 256-byte synthetic EC binary fixture."""
    data = bytearray(256)
    data[0x03] = 0x21  # Unlock register baseline (0x21)
    data[0x10] = 0x00  # CoolBoost baseline (0x00)
    data[0x13] = 0xE4  # Fan1 RPM low (2788)
    data[0x14] = 0x0A  # Fan1 RPM high
    data[0x15] = 0x90  # Fan2 RPM low (2448)
    data[0x16] = 0x09  # Fan2 RPM high
    data[0x21] = 0x00  # GPU mode baseline (0x00)
    data[0x22] = 0x00  # CPU mode baseline (0x00)
    data[0x37] = 0x00  # CPU speed baseline (0x00)
    data[0x38] = 0x00
    data[0x3A] = 0x00  # GPU speed baseline (0x00)
    data[0x3B] = 0x00
    path.write_bytes(bytes(data))


def create_synthetic_sysfs_sensors(sysfs_root: Path, cpu_temp: float = 48.0, fan1: int = 2800, fan2: int = 2450) -> None:
    """Populate synthetic sysfs hwmon hierarchy with coretemp and acer sensors."""
    coretemp_dir = sysfs_root / "class" / "hwmon" / "hwmon0"
    acer_dir = sysfs_root / "class" / "hwmon" / "hwmon1"
    coretemp_dir.mkdir(parents=True, exist_ok=True)
    acer_dir.mkdir(parents=True, exist_ok=True)

    (coretemp_dir / "name").write_text("coretemp\n")
    (coretemp_dir / "temp1_label").write_text("Package id 0\n")
    (coretemp_dir / "temp1_input").write_text(f"{int(cpu_temp * 1000)}\n")

    (acer_dir / "name").write_text("acer\n")
    (acer_dir / "fan1_input").write_text(f"{fan1}\n")
    (acer_dir / "fan2_input").write_text(f"{fan2}\n")


class TestECGuardPythonBinding(unittest.TestCase):
    """Test ctypes binding to C guard shared library."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp_dir.name)
        self.ec_file = self.tmp / "fake_ec.bin"
        self.audit_log = self.tmp / "audit.log"
        create_synthetic_ec_file(self.ec_file)
        self.guard = ECGuardBinding()
        self.ec_fd = os.open(str(self.ec_file), os.O_RDWR)

    def tearDown(self) -> None:
        self.guard.cleanup()
        if self.ec_fd is not None:
            os.close(self.ec_fd)
        self.temp_dir.cleanup()

    def test_deny_unlisted_offsets(self) -> None:
        """Verify unlisted offsets and register 0x03 are strictly denied."""
        status = self.guard.init(str(DEFAULT_POLICY_PATH), str(self.audit_log))
        self.assertEqual(status, 0)

        # Offset 0x01 is unlisted: write must be rejected
        res = self.guard.write_byte(self.ec_fd, 0x01, 0x42, "test_unlisted")
        self.assertNotEqual(res, 0)
        self.assertEqual(os.pread(self.ec_fd, 1, 0x01), b"\x00")

        # Offset 0x03 is the unlock register: write must be strictly forbidden
        res = self.guard.write_byte(self.ec_fd, 0x03, 0x11, "test_unlock")
        self.assertNotEqual(res, 0)
        self.assertEqual(os.pread(self.ec_fd, 1, 0x03), b"\x21")

        # Offset 0x39 is unlisted: write must be rejected
        res = self.guard.write_byte(self.ec_fd, 0x39, 0x05, "test_unlisted")
        self.assertNotEqual(res, 0)
        self.assertEqual(os.pread(self.ec_fd, 1, 0x39), b"\x00")

    def test_deny_partial_word_writes_before_any_byte_changes(self) -> None:
        """Verify multi-byte word writes with unlisted or invalid bytes fail closed."""
        status = self.guard.init(str(DEFAULT_POLICY_PATH), str(self.audit_log))
        self.assertEqual(status, 0)

        # Word write at 0x36: 0x36 is unlisted, 0x37 is allowed
        word_data = bytes([0x00, 0x64])
        res = self.guard.pwrite(self.ec_fd, word_data, 0x36, "test_partial_low")
        self.assertNotEqual(res, 0)
        self.assertEqual(os.pread(self.ec_fd, 1, 0x36), b"\x00")
        self.assertEqual(os.pread(self.ec_fd, 1, 0x37), b"\x00")

        # Word write at 0x38: 0x38 is allowed, 0x39 is unlisted
        word_data_high = bytes([0x00, 0x10])
        res = self.guard.pwrite(self.ec_fd, word_data_high, 0x38, "test_partial_high")
        self.assertNotEqual(res, 0)
        self.assertEqual(os.pread(self.ec_fd, 1, 0x38), b"\x00")
        self.assertEqual(os.pread(self.ec_fd, 1, 0x39), b"\x00")

        # Word write at 0x37 with disallowed upper byte value
        invalid_word = bytes([0x64, 0x02])
        res = self.guard.pwrite(self.ec_fd, invalid_word, 0x37, "test_invalid_word_val")
        self.assertNotEqual(res, 0)
        self.assertEqual(os.pread(self.ec_fd, 1, 0x37), b"\x00")
        self.assertEqual(os.pread(self.ec_fd, 1, 0x38), b"\x00")

    def test_bounds_checking_offset_255(self) -> None:
        """Verify 2-byte write at offset 255 does not wrap around to offset 0."""
        status = self.guard.init(str(DEFAULT_POLICY_PATH), str(self.audit_log))
        self.assertEqual(status, 0)

        wrap_word = bytes([0x00, 0x64])
        res = self.guard.pwrite(self.ec_fd, wrap_word, 255, "test_wrap_255")
        self.assertNotEqual(res, 0)

    def test_maximum_only_policy_enforcement(self) -> None:
        """Verify only 100% fan speed (0x64) is permitted and active 0% is strictly denied."""
        status = self.guard.init(str(DEFAULT_POLICY_PATH), str(self.audit_log))
        self.assertEqual(status, 0)

        # Active zero speed write: MUST be denied by policy! (Parent Reproduction 2)
        res = self.guard.write_word(self.ec_fd, 0x37, 0, "active_zero")
        self.assertNotEqual(res, 0)
        self.assertEqual(os.pread(self.ec_fd, 1, 0x37), b"\x00")

        # Speed 30% is rejected
        res = self.guard.write_byte(self.ec_fd, 0x37, 30, "test_low_speed")
        self.assertNotEqual(res, 0)
        self.assertEqual(os.pread(self.ec_fd, 1, 0x37), b"\x00")

        # Speed 50% is rejected
        res = self.guard.write_byte(self.ec_fd, 0x37, 50, "test_mid_speed")
        self.assertNotEqual(res, 0)
        self.assertEqual(os.pread(self.ec_fd, 1, 0x37), b"\x00")

        # Speed 100% word write at 0x37 is permitted
        res = self.guard.write_word(self.ec_fd, 0x37, 100, "test_max_speed")
        self.assertEqual(res, 0)
        self.assertEqual(os.pread(self.ec_fd, 1, 0x37), bytes([100]))
        self.assertEqual(os.pread(self.ec_fd, 1, 0x38), b"\x00")

        # Speed 100% word write at 0x3A is permitted
        res = self.guard.write_word(self.ec_fd, 0x3A, 100, "test_max_speed")
        self.assertEqual(res, 0)
        self.assertEqual(os.pread(self.ec_fd, 1, 0x3A), bytes([100]))
        self.assertEqual(os.pread(self.ec_fd, 1, 0x3B), b"\x00")

    def test_cross_process_restoration_based_on_actual_bytes(self) -> None:
        """Verify parent restore_snapshot restores registers modified by a separate process.

        Directly reproduces and validates the fix for Parent Reproduction 1.
        """
        status = self.guard.init(str(DEFAULT_POLICY_PATH), str(self.audit_log))
        self.assertEqual(status, 0)

        # Parent captures baseline snapshot
        self.assertEqual(self.guard.capture_snapshot(self.ec_fd), 0)

        # Separate subprocess initializes guard and writes 100% speed to offset 0x37
        sub_code = (
            f"import os\n"
            f"from tools.nbfc_guarded_trial import ECGuardBinding, DEFAULT_POLICY_PATH\n"
            f"guard = ECGuardBinding()\n"
            f"guard.init(str(DEFAULT_POLICY_PATH), '{self.audit_log}')\n"
            f"fd = os.open('{self.ec_file}', os.O_RDWR)\n"
            f"res = guard.write_word(fd, 0x37, 100, 'child_write')\n"
            f"assert res == 0\n"
            f"os.close(fd)\n"
        )
        proc = subprocess.run([sys.executable, "-c", sub_code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Child subprocess error: {proc.stderr}")

        # Confirm file on disk now has speed 100 at 0x37
        self.assertEqual(os.pread(self.ec_fd, 1, 0x37), bytes([100]))

        # Parent calls restore_snapshot: must restore 0x37 back to 0 based on actual bytes!
        restore_status = self.guard.restore_snapshot(self.ec_fd)
        self.assertEqual(restore_status, 0)
        self.assertEqual(os.pread(self.ec_fd, 1, 0x37), b"\x00")

        # Verify snapshot matches 100%
        verify_status = self.guard.verify_snapshot(self.ec_fd)
        self.assertEqual(verify_status, 0)

    def test_audit_log_intent_and_outcome(self) -> None:
        """Verify audit log records PROPOSED intent, ALLOWED outcome, and DENIED reason."""
        status = self.guard.init(str(DEFAULT_POLICY_PATH), str(self.audit_log))
        self.assertEqual(status, 0)

        # Allowed write: CPU mode manual 0x0C at 0x22
        self.guard.write_byte(self.ec_fd, 0x22, 0x0C, "test_cpu_manual")
        # Denied write: Unlock 0x03
        self.guard.write_byte(self.ec_fd, 0x03, 0x11, "test_unlock")
        self.guard.cleanup()

        self.assertTrue(self.audit_log.exists())
        lines = [json.loads(line) for line in self.audit_log.read_text().splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 3)

        proposed_entry = next(entry for entry in lines if entry["action"] == "PROPOSED")
        self.assertEqual(proposed_entry["offset"], "0x22")

        allowed_entry = next(entry for entry in lines if entry["action"] == "ALLOWED")
        self.assertEqual(allowed_entry["offset"], "0x22")
        self.assertEqual(allowed_entry["old"], ["0x00"])
        self.assertEqual(allowed_entry["new"], ["0x0C"])

        denied_entry = next(entry for entry in lines if entry["action"] == "DENIED")
        self.assertEqual(denied_entry["offset"], "0x03")
        self.assertEqual(denied_entry["reason"], "forbidden_unlock_register_0x03")


class TestGuardedTrialSupervisor(unittest.TestCase):
    """Test supervisor preflight, synthetic sandbox, watchdog aborts, and state isolation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp_dir.name)
        self.sandbox = self.tmp / "sandbox"
        self.sandbox.mkdir()
        self.ec_file = self.sandbox / "fake_ec.bin"
        create_synthetic_ec_file(self.ec_file)
        self.sysfs_root = self.sandbox / "sys"
        create_synthetic_sysfs_sensors(self.sysfs_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_synthetic_sandbox_mode_success(self) -> None:
        """Verify full synthetic sandbox run with synthetic sensors and EC fixture."""
        fake_service = self.sandbox / "fake_guarded_service.sh"
        fake_service.write_text("#!/bin/sh\nexec sleep 60\n")
        fake_service.chmod(0o755)

        supervisor = GuardedTrialSupervisor(
            duration_s=2,
            synthetic_root=self.sandbox,
            sysfs_root=self.sysfs_root,
            service_bin=fake_service,
        )

        exit_code = supervisor.run()
        self.assertEqual(exit_code, 0)
        self.assertTrue((self.sandbox / "trial_telemetry.jsonl").exists())
        self.assertTrue((self.sandbox / "ec_guard_audit.log").exists())

    def test_synthetic_mode_rejects_real_ec_path(self) -> None:
        """Verify synthetic mode strictly forbids pointing to real hardware paths."""
        supervisor = GuardedTrialSupervisor(
            duration_s=2,
            synthetic_root=self.sandbox,
            ec_path="/sys/kernel/debug/ec/ec0/io",
        )
        with self.assertRaises(PermissionError):
            supervisor.preflight_checks()

    def test_synthetic_mode_rejects_non_256_byte_ec(self) -> None:
        """Verify synthetic mode requires exactly 256-byte EC fixture."""
        bad_ec = self.sandbox / "bad_ec.bin"
        bad_ec.write_bytes(b"\x00" * 128)

        supervisor = GuardedTrialSupervisor(
            duration_s=2,
            synthetic_root=self.sandbox,
            ec_path=str(bad_ec),
        )
        with self.assertRaises(ValueError):
            supervisor.preflight_checks()

    def test_single_trial_lock_prevents_concurrent_runs(self) -> None:
        """Verify exclusive file lock prevents two supervisors from running concurrently."""
        supervisor1 = GuardedTrialSupervisor(
            duration_s=2,
            synthetic_root=self.sandbox,
            sysfs_root=self.sysfs_root,
        )
        supervisor1.acquire_trial_lock()

        supervisor2 = GuardedTrialSupervisor(
            duration_s=2,
            synthetic_root=self.sandbox,
            sysfs_root=self.sysfs_root,
        )
        with self.assertRaises(RuntimeError):
            supervisor2.acquire_trial_lock()

        supervisor1.cleanup()

    def test_preflight_fails_on_missing_sensors(self) -> None:
        """Verify preflight aborts if sensors are missing or returning invalid data."""
        empty_sysfs = self.sandbox / "empty_sysfs"
        empty_sysfs.mkdir()

        supervisor = GuardedTrialSupervisor(
            duration_s=2,
            synthetic_root=self.sandbox,
            sysfs_root=empty_sysfs,
        )
        with self.assertRaises(RuntimeError):
            supervisor.preflight_checks()

    def test_watchdog_aborts_on_zero_rpm_after_grace(self) -> None:
        """Verify watchdog aborts trial if tachometer reports 0 RPM after grace period."""
        # Create sensors where tachometer reports 0 RPM
        bad_sysfs = self.sandbox / "bad_sysfs"
        create_synthetic_sysfs_sensors(bad_sysfs, cpu_temp=50.0, fan1=0, fan2=0)

        fake_service = self.sandbox / "fake_service.sh"
        fake_service.write_text("#!/bin/sh\nsleep 10\nexit 0\n")
        fake_service.chmod(0o755)

        supervisor = GuardedTrialSupervisor(
            duration_s=5,
            synthetic_root=self.sandbox,
            sysfs_root=bad_sysfs,
            service_bin=fake_service,
        )

        # Run trial: watchdog should abort due to 0 RPM tachometers
        exit_code = supervisor.run()
        self.assertEqual(exit_code, 1)

    def test_child_premature_exit_returns_nonzero(self) -> None:
        """Verify that child dying prematurely causes supervisor to return nonzero."""
        fake_crashing_service = self.sandbox / "crashing_service.sh"
        fake_crashing_service.write_text("#!/bin/sh\nexit 1\n")
        fake_crashing_service.chmod(0o755)

        supervisor = GuardedTrialSupervisor(
            duration_s=5,
            synthetic_root=self.sandbox,
            sysfs_root=self.sysfs_root,
            service_bin=fake_crashing_service,
        )

        exit_code = supervisor.run()
        self.assertEqual(exit_code, 1)

    def test_state_isolation_and_restoration(self) -> None:
        """Verify existing state file is backed up uniquely and restored after trial."""
        state_file = self.sandbox / "state.json"
        original_state = {
            "SelectedConfigId": "original_model.json",
            "TargetFanSpeeds": [25.0, 25.0],
        }
        state_file.write_text(json.dumps(original_state))

        supervisor = GuardedTrialSupervisor(
            duration_s=2,
            synthetic_root=self.sandbox,
            sysfs_root=self.sysfs_root,
            state_file=state_file,
        )

        supervisor.isolate_existing_state()

        # Active state should now be 100%
        active_state = json.loads(state_file.read_text())
        self.assertEqual(active_state["TargetFanSpeeds"], [100.0, 100.0])

        # Restore isolated state
        supervisor.restore_isolated_state()
        restored_state = json.loads(state_file.read_text())
        self.assertEqual(restored_state["TargetFanSpeeds"], [25.0, 25.0])

    def test_baseline_mismatch_aborts_without_writes(self) -> None:
        """Verify that baseline mismatch against policy safe baselines aborts without writes."""
        # Mutate register 0x03 to 0x00 (expected is 0x21)
        data = bytearray(self.ec_file.read_bytes())
        data[0x03] = 0x00
        self.ec_file.write_bytes(bytes(data))

        supervisor = GuardedTrialSupervisor(
            duration_s=2,
            synthetic_root=self.sandbox,
            sysfs_root=self.sysfs_root,
        )

        supervisor.open_ec()
        with self.assertRaises(RuntimeError):
            supervisor.capture_and_verify_baseline()

        # Registers must not have been mutated
        self.assertEqual(os.pread(supervisor.ec_fd, 1, 0x37), b"\x00")
        supervisor.cleanup()

    def test_hardware_identity_verification_success_an515_54_v133(self) -> None:
        """Verify that Acer Nitro AN515-54 on verified BIOS V1.33 passes identity check."""
        dmi_dir = self.sandbox / "dmi_test"
        dmi_dir.mkdir(parents=True, exist_ok=True)
        (dmi_dir / "sys_vendor").write_text("Acer\n")
        (dmi_dir / "product_name").write_text("Nitro AN515-54\n")
        (dmi_dir / "bios_version").write_text("V1.33\n")

        supervisor = GuardedTrialSupervisor(
            synthetic_root=self.sandbox,
            sysfs_root=self.sysfs_root,
            dmi_root=dmi_dir,
        )
        match = supervisor._verify_hardware_identity()
        self.assertTrue(match.matched)
        self.assertEqual(match.model_id, "AN515-54")
        self.assertTrue(match.capabilities.guarded_max_trial)

    def test_hardware_identity_verification_rejects_wrong_bios(self) -> None:
        """Verify that an unverified BIOS version on AN515-54 fails closed for trial writes."""
        dmi_dir = self.sandbox / "dmi_test_wrong_bios"
        dmi_dir.mkdir(parents=True, exist_ok=True)
        (dmi_dir / "sys_vendor").write_text("Acer\n")
        (dmi_dir / "product_name").write_text("Nitro AN515-54\n")
        (dmi_dir / "bios_version").write_text("V1.30\n")

        supervisor = GuardedTrialSupervisor(
            synthetic_root=self.sandbox,
            sysfs_root=self.sysfs_root,
            dmi_root=dmi_dir,
        )
        with self.assertRaises(RuntimeError) as ctx:
            supervisor._verify_hardware_identity()
        self.assertIn("not eligible for guarded trial", str(ctx.exception))
        self.assertIn("upstream_only", str(ctx.exception))

    def test_hardware_identity_verification_rejects_needs_profile_model(self) -> None:
        """Verify that models without upstream profiles cannot authorize trial writes."""
        dmi_dir = self.sandbox / "dmi_test_55"
        dmi_dir.mkdir(parents=True, exist_ok=True)
        (dmi_dir / "sys_vendor").write_text("Acer\n")
        (dmi_dir / "product_name").write_text("Nitro AN515-55\n")
        (dmi_dir / "bios_version").write_text("V1.00\n")

        supervisor = GuardedTrialSupervisor(
            synthetic_root=self.sandbox,
            sysfs_root=self.sysfs_root,
            dmi_root=dmi_dir,
        )
        with self.assertRaises(RuntimeError) as ctx:
            supervisor._verify_hardware_identity()
        self.assertIn("not eligible for guarded trial", str(ctx.exception))
        self.assertIn("needs_profile", str(ctx.exception))

    def test_hardware_identity_verification_rejects_near_miss(self) -> None:
        """Verify that near-miss product strings fail closed."""
        dmi_dir = self.sandbox / "dmi_test_near_miss"
        dmi_dir.mkdir(parents=True, exist_ok=True)
        (dmi_dir / "sys_vendor").write_text("Acer\n")
        (dmi_dir / "product_name").write_text("Nitro AN515-540\n")
        (dmi_dir / "bios_version").write_text("V1.33\n")

        supervisor = GuardedTrialSupervisor(
            synthetic_root=self.sandbox,
            sysfs_root=self.sysfs_root,
            dmi_root=dmi_dir,
        )
        with self.assertRaises(RuntimeError) as ctx:
            supervisor._verify_hardware_identity()
        self.assertIn("Hardware identity differs from the model catalogue", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
