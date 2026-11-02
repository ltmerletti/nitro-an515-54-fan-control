"""
test_nbfc_native_backend.py - Integration tests running real patched NBFC binaries.

Verifies:
- Fixed combined speeds (25, 30, 60, 100%) reach EC low bytes.
- Atomic rejection of unequal fan speeds.
- Firmware Auto restoration.
- Parent pipe closure (EOF / SIGKILL) restores baseline.
- Guardian SIGKILL followed by external recovery.
- Native child process death detection.
- CPU sensor loss fails safe to baseline.
- Overtemp emergency at >= 90C with hysteresis clearing at <= 80C.
- Stale state from previous boot does not restore manual control.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time
import unittest

from nitrofan.backends.nbfc import NBFCBackend
from nitrofan.config import NBFCConfig
from nitrofan.nbfc_guardian import (
    NBFCGuardian,
    compute_file_sha256,
    get_boot_id,
    run_recovery,
    write_durable_marker,
)
from tests.test_nbfc_guarded_trial import (
    create_synthetic_ec_file,
    create_synthetic_sysfs_sensors,
)

MANIFEST_ENV = os.environ.get("NITROFAN_NBFC_MANIFEST")
REPO_ROOT = Path(__file__).resolve().parent.parent


def await_condition(predicate, timeout: float = 10.0, step: float = 0.05) -> bool:
    """Poll predicate until True or deadline expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


@unittest.skipUnless(
    MANIFEST_ENV and sys.platform == "linux",
    "Requires opt-in native Linux build via NITROFAN_NBFC_MANIFEST",
)
class TestNBFCNativeBackend(unittest.TestCase):
    """Integration test suite executing the pinned patched NBFC service."""

    def setUp(self) -> None:
        self.manifest_path = Path(MANIFEST_ENV).resolve()
        self.manifest = json.loads(self.manifest_path.read_text())
        self.runtime_dir = Path(self.manifest["runtime_root"]).resolve()
        self.policy_path = (REPO_ROOT / "tools/ec_guard/config/ec_guard_control_policy.json").resolve()
        self.profile_path = (REPO_ROOT / "profiles/Acer_Nitro_AN515-54_guarded_control.json").resolve()
        if not self.profile_path.exists():
            self.profile_path = (REPO_ROOT / "tools/ec_guard/profiles/Acer_Nitro_AN515-54_guarded_control.json").resolve()

        self.ec_file = self.runtime_dir / "fake_ec.bin"
        create_synthetic_ec_file(self.ec_file)
        self.initial_baseline = self.ec_file.read_bytes()

        self.sysfs_dir = self.runtime_dir / "sys"
        create_synthetic_sysfs_sensors(self.sysfs_dir, cpu_temp=45.0, fan1=2800, fan2=2450)

        self.nbfc_config = NBFCConfig(
            manifest_path=str(self.manifest_path),
            runtime_dir=str(self.runtime_dir),
            policy_path=str(self.policy_path),
            profile_path=str(self.profile_path),
            ec_path=str(self.ec_file),
            sysfs_root=str(self.sysfs_dir),
            synthetic_root=str(self.runtime_dir),
        )

        self.processes: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        # Terminate all spawned process groups cleanly even when a test fails.
        for proc in self.processes:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    proc.wait(timeout=2.0)
                except Exception:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass

        # Clean residual runtime session marker.
        marker = self.runtime_dir / "session.marker"
        if marker.exists():
            run_recovery(
                runtime_dir=self.runtime_dir,
                manifest_path=self.manifest_path,
                policy_path=self.policy_path,
                synthetic_root=self.runtime_dir,
                ec_path=self.ec_file,
            )

    def track_process(self, proc: subprocess.Popen) -> subprocess.Popen:
        self.processes.append(proc)
        return proc

    def test_guarded_native_cannot_detach_from_guardian(self) -> None:
        result = subprocess.run([self.manifest['service_binary_path'], '--fork'],
                                env=dict(os.environ, NBFC_EC_PATH=str(self.ec_file),
                                         NBFC_SYNTHETIC_ROOT=str(self.runtime_dir)),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn('foreground under its guardian', result.stdout + result.stderr)
        self.assertEqual(self.ec_file.read_bytes(), self.initial_baseline)

    def test_speeds_reach_ec_lowbytes_and_equal_modes(self) -> None:
        backend = NBFCBackend(
            config=self.nbfc_config,
            synthetic_root=self.runtime_dir,
            ec_path=self.ec_file,
            sysfs_root=self.sysfs_dir,
            runtime_dir=self.runtime_dir,
            policy_path=self.policy_path,
            profile_path=self.profile_path,
        )
        backend.open()
        if backend._guardian_proc is not None:
            self.track_process(backend._guardian_proc)

        try:
            # Rejection of unequal fan speeds.
            with self.assertRaises(ValueError):
                backend.set_targets({"fan1": 30, "fan2": 50})

            # Rejection of out of range speeds.
            with self.assertRaises(ValueError):
                backend.set_targets({"fan1": 15, "fan2": 15})
            with self.assertRaises(ValueError):
                backend.set_targets({"fan1": 105, "fan2": 105})

            # Verify speeds 25, 30, 60, 100% reach EC low bytes.
            for target in (25, 30, 60, 100):
                backend.set_targets({"fan1": target, "fan2": target})

                # Await active writes to EC registers 0x37 and 0x3A.
                def ec_has_target():
                    data = self.ec_file.read_bytes()
                    return data[0x37] == target and data[0x3A] == target

                reached = await_condition(ec_has_target, timeout=12.0)
                self.assertTrue(reached, f"Target speed {target}% did not reach EC within timeout")

                ec_data = self.ec_file.read_bytes()
                self.assertEqual(ec_data[0x37], target)
                self.assertEqual(ec_data[0x3A], target)
                self.assertEqual(ec_data[0x22], 0x0C)
                self.assertEqual(ec_data[0x21], 0x30)
                self.assertEqual(ec_data[0x03], 0x21)

            # Auto restores baseline.
            backend.set_auto()
            def ec_has_auto():
                data = self.ec_file.read_bytes()
                return data[0x37] == 0x00 and data[0x3A] == 0x00 and data[0x22] == 0x00 and data[0x21] == 0x00

            restored = await_condition(ec_has_auto, timeout=8.0)
            self.assertTrue(restored, "Auto did not restore baseline registers")
            self.assertEqual(self.ec_file.read_bytes(), self.initial_baseline)
        finally:
            backend.close()

    def test_forced_native_and_guardian_death_then_fresh_manual_control(self) -> None:
        """Exercise whole-service SIGKILL artifacts across two guardian lifetimes."""
        run_dir = self.runtime_dir / 'run'
        run_dir.mkdir(exist_ok=True)
        stale_pid = run_dir / 'nbfc_service.pid'
        stale_socket = run_dir / 'nbfc_service.socket'
        # The guarded executable must never depend on these upstream IPC files,
        # including when a prior native process could not run its atexit hooks.
        stale_pid.write_text('99999999')
        stale_socket.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
            endpoint.bind(str(stale_socket))
        try:
            first = NBFCBackend(config=self.nbfc_config)
            first.open()
            self.track_process(first._guardian_proc)
            first.set_targets({'fan1': 100, 'fan2': 100})
            self.assertTrue(await_condition(lambda: self.ec_file.read_bytes()[0x37] == 100))
            marker = json.loads((self.runtime_dir / 'session.marker').read_text())
            os.kill(first._guardian_proc.pid, signal.SIGKILL)
            os.kill(marker['nbfc_pid'], signal.SIGKILL)
            first._guardian_proc.wait(timeout=3)
            first.close()  # Independent recovery, before opening a new guardian.
            self.assertEqual(self.ec_file.read_bytes(), self.initial_baseline)
            second = NBFCBackend(config=self.nbfc_config)
            second.open()
            self.track_process(second._guardian_proc)
            try:
                second.set_targets({'fan1': 60, 'fan2': 60})
                self.assertTrue(await_condition(lambda: self.ec_file.read_bytes()[0x37] == 60))
                second.check_health()
                self.assertEqual(second.read_percent('fan1'), 60)
                self.assertEqual(stale_pid.read_text(), '99999999')
                self.assertTrue(stale_socket.is_socket())
            finally:
                second.close()
            self.assertEqual(self.ec_file.read_bytes(), self.initial_baseline)
        finally:
            stale_pid.unlink(missing_ok=True)
            stale_socket.unlink(missing_ok=True)

    def test_parent_pipe_eof_restores_baseline(self) -> None:
        r_parent_to_child, w_parent_to_child = os.pipe()
        r_child_to_parent, w_child_to_parent = os.pipe()

        cmd = [
            sys.executable,
            "-m",
            "nitrofan.nbfc_guardian",
            "--guardian",
            "--runtime-dir",
            str(self.runtime_dir),
            "--manifest",
            str(self.manifest_path),
            "--policy",
            str(self.policy_path),
            "--profile",
            str(self.profile_path),
            "--ec-path",
            str(self.ec_file),
            "--sysfs-root",
            str(self.sysfs_dir),
            "--synthetic-root",
            str(self.runtime_dir),
            "--parent-in-fd",
            str(r_parent_to_child),
            "--parent-out-fd",
            str(w_child_to_parent),
        ]

        proc = self.track_process(
            subprocess.Popen(
                cmd,
                pass_fds=(r_parent_to_child, w_child_to_parent),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        os.close(r_parent_to_child)
        os.close(w_child_to_parent)

        try:
            ready_line = os.read(r_child_to_parent, 1024)
            self.assertIn(b"ready", ready_line)

            os.write(w_parent_to_child, b'{"cmd": "set_targets", "percent": 60}\n')
            reply = os.read(r_child_to_parent, 1024)
            self.assertIn(b'"ok": true', reply)

            # Wait for 60% in EC.
            reached = await_condition(lambda: self.ec_file.read_bytes()[0x37] == 60, timeout=10.0)
            self.assertTrue(reached)

            # Simulate parent crash by closing the pipe.
            os.close(w_parent_to_child)

            # Guardian must detect EOF and exit cleanly.
            proc.wait(timeout=8.0)
            self.assertEqual(proc.returncode, 0)

            # Baseline registers must be restored.
            self.assertEqual(self.ec_file.read_bytes(), self.initial_baseline)
        finally:
            os.close(r_child_to_parent)

    def test_guardian_sigkill_and_external_recover(self) -> None:
        r_parent_to_child, w_parent_to_child = os.pipe()
        r_child_to_parent, w_child_to_parent = os.pipe()

        cmd = [
            sys.executable,
            "-m",
            "nitrofan.nbfc_guardian",
            "--guardian",
            "--runtime-dir",
            str(self.runtime_dir),
            "--manifest",
            str(self.manifest_path),
            "--policy",
            str(self.policy_path),
            "--profile",
            str(self.profile_path),
            "--ec-path",
            str(self.ec_file),
            "--sysfs-root",
            str(self.sysfs_dir),
            "--synthetic-root",
            str(self.runtime_dir),
            "--parent-in-fd",
            str(r_parent_to_child),
            "--parent-out-fd",
            str(w_child_to_parent),
        ]

        proc = self.track_process(
            subprocess.Popen(
                cmd,
                pass_fds=(r_parent_to_child, w_child_to_parent),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        os.close(r_parent_to_child)
        os.close(w_child_to_parent)

        try:
            os.read(r_child_to_parent, 1024)
            os.write(w_parent_to_child, b'{"cmd": "set_targets", "percent": 100}\n')
            os.read(r_child_to_parent, 1024)

            reached = await_condition(lambda: self.ec_file.read_bytes()[0x37] == 100, timeout=10.0)
            self.assertTrue(reached)

            marker = self.runtime_dir / "session.marker"
            self.assertTrue(marker.exists())

            # Terminate guardian abruptly with SIGKILL.
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3.0)

            # EC remains at 100% and marker persists.
            self.assertEqual(self.ec_file.read_bytes()[0x37], 100)
            self.assertTrue(marker.exists())

            # External recovery restores baseline.
            rec_code = run_recovery(
                runtime_dir=self.runtime_dir,
                manifest_path=self.manifest_path,
                policy_path=self.policy_path,
                synthetic_root=self.runtime_dir,
                ec_path=self.ec_file,
            )
            self.assertEqual(rec_code, 0)
            self.assertFalse(marker.exists())
            self.assertEqual(self.ec_file.read_bytes(), self.initial_baseline)
        finally:
            os.close(w_parent_to_child)
            os.close(r_child_to_parent)

    def test_native_child_death_restores_baseline(self) -> None:
        backend = NBFCBackend(
            config=self.nbfc_config,
            synthetic_root=self.runtime_dir,
            ec_path=self.ec_file,
            sysfs_root=self.sysfs_dir,
            runtime_dir=self.runtime_dir,
            policy_path=self.policy_path,
            profile_path=self.profile_path,
        )
        backend.open()
        if backend._guardian_proc is not None:
            self.track_process(backend._guardian_proc)

        try:
            backend.set_targets({"fan1": 50, "fan2": 50})
            reached = await_condition(lambda: self.ec_file.read_bytes()[0x37] == 50, timeout=10.0)
            self.assertTrue(reached)

            marker_path = self.runtime_dir / "session.marker"
            marker_data = json.loads(marker_path.read_text())
            child_pid = marker_data.get("nbfc_pid")
            self.assertIsNotNone(child_pid)

            # Send SIGKILL to the native nbfc_service child.
            os.kill(child_pid, signal.SIGKILL)

            # Guardian health check or tick must detect child exit and restore Auto baseline.
            def baseline_restored():
                return self.ec_file.read_bytes() == self.initial_baseline

            restored = await_condition(baseline_restored, timeout=10.0)
            self.assertTrue(restored, "Baseline was not restored after native child death")
        finally:
            backend.close()

    def test_cpu_sensor_loss_restores_baseline(self) -> None:
        backend = NBFCBackend(
            config=self.nbfc_config,
            synthetic_root=self.runtime_dir,
            ec_path=self.ec_file,
            sysfs_root=self.sysfs_dir,
            runtime_dir=self.runtime_dir,
            policy_path=self.policy_path,
            profile_path=self.profile_path,
        )
        backend.open()
        if backend._guardian_proc is not None:
            self.track_process(backend._guardian_proc)

        try:
            backend.set_targets({"fan1": 50, "fan2": 50})
            reached = await_condition(lambda: self.ec_file.read_bytes()[0x37] == 50, timeout=10.0)
            self.assertTrue(reached)

            # Unlink the CPU temperature input file.
            cpu_sensor_file = self.sysfs_dir / "class/hwmon/hwmon0/temp1_input"
            cpu_sensor_file.unlink()

            # Guardian sensor check detects 3 consecutive missing ticks and restores baseline.
            def baseline_restored():
                return self.ec_file.read_bytes() == self.initial_baseline

            restored = await_condition(baseline_restored, timeout=12.0)
            self.assertTrue(restored, "Baseline was not restored after CPU sensor loss")
        finally:
            backend.close()

    def test_overtemp_emergency_and_hysteresis(self) -> None:
        backend = NBFCBackend(
            config=self.nbfc_config,
            synthetic_root=self.runtime_dir,
            ec_path=self.ec_file,
            sysfs_root=self.sysfs_dir,
            runtime_dir=self.runtime_dir,
            policy_path=self.policy_path,
            profile_path=self.profile_path,
        )
        backend.open()
        if backend._guardian_proc is not None:
            self.track_process(backend._guardian_proc)

        try:
            backend.set_targets({"fan1": 40, "fan2": 40})
            reached = await_condition(lambda: self.ec_file.read_bytes()[0x37] == 40, timeout=10.0)
            self.assertTrue(reached)

            # Trip emergency with temperature 92C.
            create_synthetic_sysfs_sensors(self.sysfs_dir, cpu_temp=92.0)

            def is_emergency_speed():
                return self.ec_file.read_bytes()[0x37] == 100

            tripped = await_condition(is_emergency_speed, timeout=10.0)
            self.assertTrue(tripped, "Emergency 100% was not triggered by 92C")

            # Lower temperature to 85C (hysteresis keeps emergency 100%).
            create_synthetic_sysfs_sensors(self.sysfs_dir, cpu_temp=85.0)
            time.sleep(2.0)
            self.assertEqual(self.ec_file.read_bytes()[0x37], 100)

            # Lower temperature to 78C (<= 80C clears emergency and restores 40%).
            create_synthetic_sysfs_sensors(self.sysfs_dir, cpu_temp=78.0)

            def is_normal_speed():
                return self.ec_file.read_bytes()[0x37] == 40

            cleared = await_condition(is_normal_speed, timeout=10.0)
            self.assertTrue(cleared, "Emergency was not cleared after cooling to 78C")
        finally:
            backend.close()

    def test_close_during_emergency_reaps_native_and_restores_baseline(self) -> None:
        backend = NBFCBackend(config=self.nbfc_config)
        backend.open()
        guardian = self.track_process(backend._guardian_proc)
        try:
            backend.set_targets({'fan1': 60, 'fan2': 60})
            self.assertTrue(await_condition(lambda: self.ec_file.read_bytes()[0x37] == 60))
            create_synthetic_sysfs_sensors(self.sysfs_dir, cpu_temp=92.0)
            self.assertTrue(await_condition(lambda: self.ec_file.read_bytes()[0x37] == 100))
            backend.check_health()
            self.assertTrue(backend.emergency)
        finally:
            backend.close()
        self.assertEqual(guardian.returncode, 0, 'Guardian must complete its own shutdown')
        self.assertFalse((self.runtime_dir / 'session.marker').exists())
        self.assertEqual(self.ec_file.read_bytes(), self.initial_baseline)

    def test_stale_state_does_not_restore_manual(self) -> None:
        marker = self.runtime_dir / "session.marker"
        marker_data = {
            "version": 1,
            "boot_id": "old-rebooted-boot-id-9999",
            "manifest_sha256": compute_file_sha256(self.manifest_path),
            "policy_sha256": compute_file_sha256(self.policy_path),
            "profile_sha256": compute_file_sha256(self.profile_path),
            "ec_path": str(self.ec_file),
            "guardian_pid": 99999,
            "nbfc_pid": None,
            "timestamp": time.time(),
            "active": True,
            "baseline_captured": True,
            "is_synthetic": True,
        }
        write_durable_marker(marker, marker_data)

        rec_code = run_recovery(
            runtime_dir=self.runtime_dir,
            manifest_path=self.manifest_path,
            policy_path=self.policy_path,
            synthetic_root=self.runtime_dir,
            ec_path=self.ec_file,
        )
        self.assertEqual(rec_code, 0)
        self.assertFalse(marker.exists())
        self.assertEqual(self.ec_file.read_bytes(), self.initial_baseline)


if __name__ == "__main__":
    unittest.main()
