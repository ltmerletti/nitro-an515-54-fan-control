"""
test_nbfc_backend.py - Unit tests for NitroFan NBFC backend and guardian logic.

Verifies:
- Config validation and rejection of invalid startup_mode or fields.
- Fixed combined speeds and mode tracking.
- Atomic rejection of software curves and unequal fan speeds.
- Guardian parent pipe EOF handling.
- Marker handling: no-marker, stale or tampered marker, boot ID mismatch.
- EC baseline mismatch zero writes refusal via C guard snapshot.
- Guardian safety monitoring: emergency thermal trip, hysteresis, sensor loss, fan stall.
- Native child death detection and auto fail-safe restoration.
- Recovery from active durable marker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from nitrofan.backends.nbfc import NBFCBackend
from nitrofan.config import ConfigError, NBFCConfig, load_config, parse_config
from nitrofan.controller import Controller
from nitrofan.nbfc_guardian import (
    DEFAULT_PROFILE_PATH,
    NBFCGuardian,
    compute_file_sha256,
    get_boot_id,
    run_recovery,
    write_durable_marker,
)
from nitrofan.sensors import SystemSensorProvider
from tests.test_nbfc_guarded_trial import (
    create_synthetic_ec_file,
    create_synthetic_sysfs_sensors,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / "tools" / "ec_guard" / "config" / "ec_guard_control_policy.json"
PROFILE_PATH = REPO_ROOT / "tools" / "ec_guard" / "profiles" / "Acer_Nitro_AN515-54_guarded_control.json"
SHARED_LIB_NAME = "libnbfc_ec_guard.dylib" if sys.platform == "darwin" else "libnbfc_ec_guard.so"
LIB_PATH = REPO_ROOT / "tools" / "ec_guard" / SHARED_LIB_NAME


class TestNBFCConfig(unittest.TestCase):
    """Test configuration validation for the NBFC backend."""

    def test_load_example_nbfc_config(self) -> None:
        cfg = load_config(REPO_ROOT / "examples" / "nbfc.toml")
        self.assertEqual(cfg.backend, "nbfc")
        self.assertEqual(cfg.startup_mode, "auto")
        self.assertIsNotNone(cfg.nbfc)
        self.assertEqual(cfg.nbfc.manifest_path, "/opt/nitrofan/build/manifest.json")
        self.assertEqual(cfg.nbfc.runtime_dir, "/opt/nitrofan/runtime")

    def test_reject_non_auto_startup_mode(self) -> None:
        raw = {
            "backend": "nbfc",
            "tick_s": 2.0,
            "reassert_s": 10.0,
            "min_percent": 25,
            "emergency_c": 90,
            "startup_mode": "curve",
        }
        with self.assertRaises(ConfigError) as ctx:
            parse_config(raw)
        self.assertIn("startup_mode 'auto'", str(ctx.exception))

    def test_reject_invalid_nbfc_fields(self) -> None:
        raw = {
            "backend": "nbfc",
            "tick_s": 2.0,
            "reassert_s": 10.0,
            "min_percent": 25,
            "emergency_c": 90,
            "startup_mode": "auto",
            "nbfc": {
                "manifest_path": "",
            },
        }
        with self.assertRaises(ConfigError) as ctx:
            parse_config(raw)
        self.assertIn("manifest_path", str(ctx.exception))


class TestNBFCBackendUnit(unittest.TestCase):
    """Unit tests for NBFCBackend command dispatch and input validation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="nbfc-backend-test-")
        self.sandbox = Path(self.temp_dir).resolve()
        self.runtime_dir = self.sandbox / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        self.ec_file = self.sandbox / "fake_ec.bin"
        create_synthetic_ec_file(self.ec_file)

        self.sysfs_dir = self.sandbox / "sys"
        create_synthetic_sysfs_sensors(self.sysfs_dir, cpu_temp=45.0, fan1=2788, fan2=2448)

        self.manifest_file = self.sandbox / "manifest.json"
        self.manifest_file.write_text(json.dumps({"service_binary_sha256": "dummy"}))

        self.nbfc_config = NBFCConfig(
            manifest_path=str(self.manifest_file),
            runtime_dir=str(self.runtime_dir),
            policy_path=str(POLICY_PATH),
            profile_path=str(PROFILE_PATH),
            ec_path=str(self.ec_file),
            sysfs_root=str(self.sysfs_dir),
            synthetic_root=str(self.sandbox),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def test_speeds_and_exact_auto_restore(self) -> None:
        backend = NBFCBackend(
            config=self.nbfc_config,
            synthetic_root=self.sandbox,
            ec_path=self.ec_file,
            sysfs_root=self.sysfs_dir,
            runtime_dir=self.runtime_dir,
            policy_path=POLICY_PATH,
            profile_path=PROFILE_PATH,
        )

        sent_commands = []

        def mock_send_command(cmd):
            sent_commands.append(cmd)
            if cmd.get("cmd") == "get_status":
                return {
                    "ok": True,
                    "percent": backend._current_percent,
                    "emergency": False,
                    "rpms": {"fan1": 2800, "fan2": 2450},
                }
            return {"ok": True}

        backend._is_open = True
        backend._send_command = mock_send_command

        try:
            self.assertIsNone(backend.read_percent("fan1"))
            self.assertIsNone(backend.read_percent("fan2"))

            for target in (25, 30, 60, 100):
                backend.set_targets({"fan1": target, "fan2": target})
                self.assertEqual(backend.read_percent("fan1"), target)
                self.assertEqual(backend.read_percent("fan2"), target)
                self.assertTrue(any(cmd == {"cmd": "set_targets", "percent": target} for cmd in sent_commands))

            backend.set_auto()
            self.assertIsNone(backend.read_percent("fan1"))
            self.assertIsNone(backend.read_percent("fan2"))
            self.assertTrue(any(cmd == {"cmd": "set_auto"} for cmd in sent_commands))
        finally:
            backend._is_open = False

    def test_combined_mode_atomic_rejection(self) -> None:
        backend = NBFCBackend(
            config=self.nbfc_config,
            synthetic_root=self.sandbox,
            ec_path=self.ec_file,
            sysfs_root=self.sysfs_dir,
            runtime_dir=self.runtime_dir,
            policy_path=POLICY_PATH,
            profile_path=PROFILE_PATH,
        )
        backend._is_open = True
        try:
            with self.assertRaises(ValueError) as ctx:
                backend.set_targets({"fan1": 30, "fan2": 50})
            self.assertIn("equal combined fan speeds", str(ctx.exception))

            with self.assertRaises(ValueError):
                backend.set_targets({"fan1": 15, "fan2": 15})
            with self.assertRaises(ValueError):
                backend.set_targets({"fan1": 105, "fan2": 105})

            with self.assertRaises(ValueError):
                backend.set_targets({"cpu": 50, "gpu": 50})
        finally:
            backend._is_open = False

    def test_controller_rejects_curves_and_unequal_for_nbfc(self) -> None:
        backend = NBFCBackend(
            config=self.nbfc_config,
            synthetic_root=self.sandbox,
            ec_path=self.ec_file,
            sysfs_root=self.sysfs_dir,
            runtime_dir=self.runtime_dir,
            policy_path=POLICY_PATH,
            profile_path=PROFILE_PATH,
        )
        backend._is_open = True
        backend._send_command = lambda cmd: {
            "ok": True,
            "percent": None,
            "emergency": False,
            "rpms": {"fan1": 2800, "fan2": 2450},
        }
        try:
            cfg = load_config(REPO_ROOT / "examples" / "nbfc.toml")
            controller = Controller(
                config=cfg,
                backend=backend,
                sensor_provider=lambda: {"cpu": 45.0, "gpu": 40.0},
                clock=time.monotonic,
            )

            with self.assertRaises(ValueError) as ctx:
                controller.set_mode("curve")
            self.assertIn("nbfc backend does not support software curves", str(ctx.exception))

            with self.assertRaises(ValueError) as ctx:
                controller.set_curve("fan1", [[45, 25], [80, 100]])
            self.assertIn("nbfc backend does not support software curves", str(ctx.exception))

            with self.assertRaises(ValueError) as ctx:
                controller.set_mode("fixed", percents={"fan1": 30, "fan2": 60})
            self.assertIn("equal combined fan speeds", str(ctx.exception))

            status = controller.get_status()
            self.assertIn("fan1", status["fans"])
            self.assertIn("requested_percent", status["fans"]["fan1"])
        finally:
            backend._is_open = False


class TestNBFCGuardianUnit(unittest.TestCase):
    """Unit tests for NBFCGuardian lifecycle, marker integrity, and safety checks."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="nbfc-guardian-test-")
        self.sandbox = Path(self.temp_dir).resolve()
        self.runtime_dir = self.sandbox / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        self.ec_file = self.sandbox / "fake_ec.bin"
        create_synthetic_ec_file(self.ec_file)

        self.sysfs_dir = self.sandbox / "sys"
        create_synthetic_sysfs_sensors(self.sysfs_dir, cpu_temp=45.0, fan1=2788, fan2=2448)

        self.manifest_file = self.sandbox / "manifest.json"
        self.manifest_file.write_text(json.dumps({"service_binary_sha256": "dummy"}))

    def tearDown(self) -> None:
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def test_no_marker_no_writes(self) -> None:
        rec_code = run_recovery(
            runtime_dir=self.runtime_dir,
            manifest_path=self.manifest_file,
            policy_path=POLICY_PATH,
            synthetic_root=self.sandbox,
            ec_path=self.ec_file,
        )
        self.assertEqual(rec_code, 0)

    def test_stale_or_tampered_marker_fails_closed(self) -> None:
        marker = self.runtime_dir / "session.marker"
        marker.write_text("{invalid json corrupt content")

        rec_code = run_recovery(
            runtime_dir=self.runtime_dir,
            manifest_path=self.manifest_file,
            policy_path=POLICY_PATH,
            synthetic_root=self.sandbox,
            ec_path=self.ec_file,
        )
        self.assertEqual(rec_code, 1)

    def test_boot_id_mismatch_skips_writes(self) -> None:
        marker = self.runtime_dir / "session.marker"
        marker_data = {
            "version": 1,
            "boot_id": "old-rebooted-boot-id-9999",
            "manifest_sha256": compute_file_sha256(self.manifest_file),
            "policy_sha256": compute_file_sha256(POLICY_PATH),
            "profile_sha256": compute_file_sha256(DEFAULT_PROFILE_PATH),
            "ec_path": str(self.ec_file),
            "guardian_pid": 99999,
            "nbfc_pid": None,
            "timestamp": time.time(),
            "active": True,
            "baseline_captured": True,
            "is_synthetic": True,
        }
        write_durable_marker(marker, marker_data)

        with patch("nitrofan.nbfc_guardian.NBFCGuardian.attest_build", autospec=True) as mock_attest:
            def fake_attest(guardian_self):
                guardian_self.library_path = LIB_PATH
            mock_attest.side_effect = fake_attest
            rec_code = run_recovery(
                runtime_dir=self.runtime_dir,
                manifest_path=self.manifest_file,
                policy_path=POLICY_PATH,
                synthetic_root=self.sandbox,
                ec_path=self.ec_file,
            )
            self.assertEqual(rec_code, 0)
            self.assertFalse(marker.exists())

    def test_guardian_sigkill_and_external_recovery(self) -> None:
        marker = self.runtime_dir / "session.marker"
        marker_data = {
            "version": 1,
            "boot_id": get_boot_id(self.sandbox),
            "manifest_sha256": compute_file_sha256(self.manifest_file),
            "policy_sha256": compute_file_sha256(POLICY_PATH),
            "profile_sha256": compute_file_sha256(DEFAULT_PROFILE_PATH),
            "ec_path": str(self.ec_file),
            "guardian_pid": 99999,
            "nbfc_pid": None,
            "timestamp": time.time(),
            "active": True,
            "baseline_captured": True,
            "is_synthetic": True,
        }
        write_durable_marker(marker, marker_data)

        with patch("nitrofan.nbfc_guardian.NBFCGuardian.attest_build", autospec=True) as mock_attest:
            def fake_attest(guardian_self):
                guardian_self.library_path = LIB_PATH
            mock_attest.side_effect = fake_attest
            rec_code = run_recovery(
                runtime_dir=self.runtime_dir,
                manifest_path=self.manifest_file,
                policy_path=POLICY_PATH,
                synthetic_root=self.sandbox,
                ec_path=self.ec_file,
            )
            self.assertEqual(rec_code, 0)
            self.assertFalse(marker.exists())

    def test_baseline_mismatch_zero_writes(self) -> None:
        ec_data = bytearray(self.ec_file.read_bytes())
        ec_data[0x03] = 0x00
        self.ec_file.write_bytes(bytes(ec_data))

        guardian = NBFCGuardian(
            runtime_dir=self.runtime_dir,
            manifest_path=self.manifest_file,
            policy_path=POLICY_PATH,
            profile_path=PROFILE_PATH,
            ec_path=self.ec_file,
            sysfs_root=self.sysfs_dir,
            synthetic_root=self.sandbox,
        )
        guardian.attest_build = Mock()
        guardian.library_path = LIB_PATH
        guardian.preflight()

        with self.assertRaises(RuntimeError) as ctx:
            guardian.init_ec_guard()
        self.assertIn("baseline mismatch", str(ctx.exception))
        guardian.cleanup()

    def test_guardian_parent_sigkill(self) -> None:
        r_parent_to_child, w_parent_to_child = os.pipe()
        r_child_to_parent, w_child_to_parent = os.pipe()

        guardian = NBFCGuardian(
            runtime_dir=self.runtime_dir,
            manifest_path=self.manifest_file,
            policy_path=POLICY_PATH,
            profile_path=PROFILE_PATH,
            ec_path=self.ec_file,
            sysfs_root=self.sysfs_dir,
            synthetic_root=self.sandbox,
            parent_in_fd=r_parent_to_child,
            parent_out_fd=w_child_to_parent,
        )
        guardian.attest_build = Mock()
        guardian.library_path = LIB_PATH

        os.close(w_parent_to_child)
        exit_code = guardian.run()
        self.assertEqual(exit_code, 0)

        os.close(r_parent_to_child)
        os.close(r_child_to_parent)
        os.close(w_child_to_parent)

    def test_guardian_safety_checks_and_emergency(self) -> None:
        guardian = NBFCGuardian(
            runtime_dir=self.runtime_dir,
            manifest_path=self.manifest_file,
            policy_path=POLICY_PATH,
            profile_path=PROFILE_PATH,
            ec_path=self.ec_file,
            sysfs_root=self.sysfs_dir,
            synthetic_root=self.sandbox,
        )
        guardian.attest_build = Mock()
        guardian.library_path = LIB_PATH
        guardian.preflight()
        guardian.init_ec_guard()

        def mock_start(pct):
            guardian.current_percent = pct
        guardian.start_or_restart_child = mock_start

        try:
            guardian.set_percent(40)
            self.assertEqual(guardian.current_percent, 40)

            # High temperature 92C forces emergency 100%
            create_synthetic_sysfs_sensors(self.sysfs_dir, cpu_temp=92.0)
            provider = guardian.get_sensor_provider()
            guardian.check_sensors(provider)
            self.assertTrue(guardian.emergency)
            self.assertEqual(guardian.current_percent, 100)
            self.assertEqual(guardian.last_temps['cpu'], 92.0)
            self.assertEqual(guardian.emergency_trigger_temps['cpu'], 92.0)

            # Hysteresis at 85C keeps emergency 100%
            create_synthetic_sysfs_sensors(self.sysfs_dir, cpu_temp=85.0)
            provider = guardian.get_sensor_provider()
            guardian.check_sensors(provider)
            self.assertTrue(guardian.emergency)
            self.assertEqual(guardian.current_percent, 100)

            # Cooling to 78C clears emergency and restores requested 40%
            create_synthetic_sysfs_sensors(self.sysfs_dir, cpu_temp=78.0)
            provider = guardian.get_sensor_provider()
            guardian.check_sensors(provider)
            self.assertFalse(guardian.emergency)
            self.assertEqual(guardian.current_percent, 40)
            self.assertEqual(guardian.last_temps['cpu'], 78.0)
            self.assertEqual(guardian.emergency_trigger_temps['cpu'], 92.0)

            # CPU sensor loss for 3 consecutive checks forces Auto
            empty_provider = SystemSensorProvider(sysfs_root=self.sandbox / "nonexistent_sys")
            guardian.check_sensors(empty_provider)
            self.assertEqual(guardian.current_percent, 40)
            guardian.check_sensors(empty_provider)
            self.assertEqual(guardian.current_percent, 40)
            guardian.check_sensors(empty_provider)
            self.assertIsNone(guardian.current_percent)
        finally:
            guardian.cleanup()

    def test_fan_zero_stall_restores_auto(self) -> None:
        guardian = NBFCGuardian(
            runtime_dir=self.runtime_dir,
            manifest_path=self.manifest_file,
            policy_path=POLICY_PATH,
            profile_path=PROFILE_PATH,
            ec_path=self.ec_file,
            sysfs_root=self.sysfs_dir,
            synthetic_root=self.sandbox,
        )
        guardian.attest_build = Mock()
        guardian.library_path = LIB_PATH
        guardian.preflight()
        guardian.init_ec_guard()

        def mock_start(pct):
            guardian.current_percent = pct
        guardian.start_or_restart_child = mock_start

        try:
            guardian.set_percent(50)
            self.assertEqual(guardian.current_percent, 50)

            # Mock tachometers reporting 0 RPM
            guardian.read_rpms = Mock(return_value={"fan1": 0, "fan2": 0})
            provider = guardian.get_sensor_provider()
            guardian.check_sensors(provider)

            self.assertIsNone(guardian.current_percent)
            self.assertIsNotNone(guardian.fault)
        finally:
            guardian.cleanup()

    def test_child_death_restores_baseline(self) -> None:
        guardian = NBFCGuardian(
            runtime_dir=self.runtime_dir,
            manifest_path=self.manifest_file,
            policy_path=POLICY_PATH,
            profile_path=PROFILE_PATH,
            ec_path=self.ec_file,
            sysfs_root=self.sysfs_dir,
            synthetic_root=self.sandbox,
        )
        guardian.attest_build = Mock()
        guardian.library_path = LIB_PATH
        guardian.preflight()
        guardian.init_ec_guard()

        def mock_start(pct):
            guardian.current_percent = pct
        guardian.start_or_restart_child = mock_start

        try:
            guardian.set_percent(60)
            self.assertEqual(guardian.current_percent, 60)

            dummy_proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(1)"])
            dummy_proc.wait()
            guardian.child_process = dummy_proc

            if guardian.child_process.poll() is not None:
                guardian.set_auto()

            self.assertIsNone(guardian.current_percent)
        finally:
            guardian.cleanup()


if __name__ == "__main__":
    unittest.main()
