"""Regression checks for control ownership and failed restoration."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from nitrofan.nbfc_guardian import NBFCGuardian
from nitrofan.backends.nbfc import NBFCBackend


class TestGuardedBackendReview(unittest.TestCase):
    def test_guardian_trigger_readings_reach_client_status(self):
        backend = NBFCBackend()
        backend._is_open = True
        safety = {'temps': {'cpu': 81}, 'trigger_temps': {'cpu': 92},
                  'emergency_sources': ['cpu']}
        with patch.object(backend, '_send_command', return_value={
                'ok': True, 'percent': 100, 'emergency': True, 'safety': safety,
                'rpms': {'fan1': 6000, 'fan2': 6000}}):
            from nitrofan.config import load_config
            from nitrofan.controller import Controller
            config = load_config(Path(__file__).resolve().parents[1] / 'examples/nbfc.toml')
            controller = Controller(config, backend, lambda: {'cpu': 51}, clock=lambda: 0.0)
            status = controller.get_status()
        self.assertTrue(status['emergency'])
        self.assertEqual(status['safety']['trigger_temps']['cpu'], 92)

    def test_rpm_communication_failure_never_returns_cached_success(self):
        backend = NBFCBackend()
        backend._is_open = True
        backend._rpm_cache = {'fan1': 6000, 'fan2': 6000}
        with patch.object(backend, '_send_command', side_effect=RuntimeError('guardian dead')):
            with self.assertRaisesRegex(RuntimeError, 'guardian dead'):
                backend.read_rpm('fan1')

    def test_partial_and_empty_target_maps_are_rejected_before_ipc(self):
        backend = NBFCBackend()
        backend._is_open = True
        with patch.object(backend, '_send_command') as send:
            for targets in ({}, {'fan1': 60}, {'fan2': 60}, {'fan1': 60, 'fan2': 40}):
                with self.assertRaises(ValueError):
                    backend.set_targets(targets)
            send.assert_not_called()

    def make_guardian(self, root):
        root = Path(root)
        (root / 'fake_ec.bin').write_bytes(bytes(256))
        return NBFCGuardian(runtime_dir=root, synthetic_root=root,
                            manifest_path=root / 'missing-manifest.json')

    def test_missing_build_manifest_fails_before_ec_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            guardian = self.make_guardian(temporary)
            try:
                with self.assertRaises((OSError, RuntimeError, ValueError)):
                    guardian.preflight()
                self.assertIsNone(guardian.ec_fd)
            finally:
                guardian.release_lock()

    def test_missing_native_binary_cannot_fall_back_to_python_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            guardian = self.make_guardian(temporary)
            guardian.guard = Mock()
            guardian.ec_fd = 123456
            try:
                with self.assertRaises((OSError, RuntimeError, ValueError)):
                    guardian.start_or_restart_child(60)
                guardian.guard.write_byte.assert_not_called()
                guardian.guard.write_word.assert_not_called()
            finally:
                guardian.ec_fd = None

    def test_failed_restoration_keeps_durable_recovery_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            guardian = self.make_guardian(temporary)
            guardian.marker_path.write_text('{"active":true}')
            guardian.guard = Mock()
            guardian.guard.restore_snapshot.return_value = -3
            guardian.guard.verify_snapshot.return_value = -1
            guardian.ec_fd = 123456
            with patch('nitrofan.nbfc_guardian.os.close'):
                try:
                    guardian.cleanup()
                except (OSError, RuntimeError):
                    pass
            self.assertTrue(guardian.marker_path.exists())


if __name__ == '__main__':
    unittest.main()
