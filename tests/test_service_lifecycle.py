"""Lifecycle failures must leave recovery possible and never resume manual mode."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import service_hardware
from tools import validate_backend


class TestServiceLifecycle(unittest.TestCase):
    def test_failed_recovery_never_unloads_ec_driver(self):
        calls = []

        def fail_recovery(*command, **kwargs):
            calls.append(command)
            raise subprocess.CalledProcessError(1, command)

        with patch.object(service_hardware, 'run', side_effect=fail_recovery):
            with self.assertRaises(subprocess.CalledProcessError):
                service_hardware.release_control()
        self.assertEqual(len(calls), 1)
        self.assertIn('--recover', calls[0])

    def test_live_native_writer_prevents_driver_unload(self):
        with patch.object(service_hardware, 'run') as run:
            with patch.object(service_hardware.subprocess, 'run',
                              return_value=subprocess.CompletedProcess([], 0)):
                with self.assertRaisesRegex(RuntimeError, 'still running'):
                    service_hardware.release_control()
        self.assertEqual(run.call_count, 1)
        self.assertIn('--recover', run.call_args.args)

    def test_failed_stop_preserves_sleep_marker_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / 'resume'
            with patch.object(service_hardware, 'SLEEP_MARKER', marker):
                with patch.object(service_hardware.subprocess, 'run',
                                  return_value=subprocess.CompletedProcess([], 0)):
                    with patch.object(service_hardware, 'run',
                                      side_effect=subprocess.TimeoutExpired('systemctl', 35)):
                        with self.assertRaises(subprocess.TimeoutExpired):
                            service_hardware.prepare_sleep()
                self.assertTrue(marker.exists())

    def test_inactive_service_is_not_started_after_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(service_hardware, 'SLEEP_MARKER', Path(temporary) / 'resume'):
                with patch.object(service_hardware.subprocess, 'run',
                                  return_value=subprocess.CompletedProcess([], 3)):
                    service_hardware.prepare_sleep()
                with patch.object(service_hardware, 'run') as run:
                    service_hardware.resume_after_sleep()
                    run.assert_not_called()

    def test_failed_resume_keeps_restart_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / 'resume'
            marker.write_text('restart\n')
            with patch.object(service_hardware, 'SLEEP_MARKER', marker):
                with patch.object(service_hardware, 'run',
                                  side_effect=subprocess.CalledProcessError(1, 'systemctl')):
                    with self.assertRaises(subprocess.CalledProcessError):
                        service_hardware.resume_after_sleep()
            self.assertTrue(marker.exists())


class TestBoundedValidation(unittest.TestCase):
    def test_sensor_loss_and_thermal_emergency_abort(self):
        healthy = {'temps': {'cpu': 50}, 'fans': {'fan1': {'rpm': 2500}, 'fan2': {'rpm': 2500}}}
        validate_backend.check_readings(healthy, 10)
        for cpu in (None, True, float('nan'), 80, 95):
            with self.subTest(cpu=cpu):
                status = {**healthy, 'temps': {'cpu': cpu}}
                with self.assertRaises(RuntimeError):
                    validate_backend.check_readings(status, 10)
        with self.assertRaises(RuntimeError):
            validate_backend.check_readings({**healthy, 'emergency': True}, 10)

    def test_stopped_fan_aborts_after_spinup_grace(self):
        status = {'temps': {'cpu': 50}, 'fans': {'fan1': {'rpm': 0}, 'fan2': {'rpm': 2500}}}
        validate_backend.check_readings(status, 1)
        with self.assertRaisesRegex(RuntimeError, 'fan1'):
            validate_backend.check_readings(status, 5)

    def test_mode_failure_still_stops_service_and_verifies_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = {f'0x{offset:02x}': 0 for offset in validate_backend.OFFSETS}
            policy = root / 'policy.json'
            import json
            policy.write_text(json.dumps({'expected_baselines': baseline}))
            config = root / 'config.toml'
            config.write_text(f'[nbfc]\npolicy_path = "{policy}"\n')
            support = root / 'write_support'
            support.write_text('N\n')
            status = {'temps': {'cpu': 50},
                      'fans': {'fan1': {'rpm': 2500}, 'fan2': {'rpm': 2500}}}
            calls = []

            def service_call(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 3 if 'is-active' in command else 0)

            with patch.object(validate_backend, 'CONFIG', config), \
                    patch.object(validate_backend, 'WRITE_SUPPORT', support), \
                    patch.object(validate_backend.subprocess, 'run', side_effect=service_call), \
                    patch.object(validate_backend, 'wait_auto', return_value=status), \
                    patch.object(validate_backend, 'read_registers', return_value=baseline), \
                    patch.object(validate_backend, 'request', side_effect=RuntimeError('lost API')), \
                    patch.object(validate_backend, 'record'):
                with self.assertRaisesRegex(RuntimeError, 'lost API'):
                    validate_backend.validate(10)
            self.assertIn(['systemctl', 'stop', 'nitrofan.service'], calls)
            self.assertEqual(calls[-1], ['systemctl', 'stop', 'nitrofan-validation-stop.timer'])
            self.assertTrue(any(command[0] == 'systemd-run' for command in calls))

    def test_get_main_pid_handles_valid_and_missing_output(self):
        with patch.object(validate_backend.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 0, stdout='4567\n')):
            self.assertEqual(validate_backend.get_main_pid(), 4567)
        with patch.object(validate_backend.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 1, stdout='')):
            self.assertEqual(validate_backend.get_main_pid(), 0)
        with patch.object(validate_backend.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 0, stdout='not-a-number\n')):
            self.assertEqual(validate_backend.get_main_pid(), 0)

    def test_wait_crash_recovery_rejects_identical_pid(self):
        baseline = {f'0x{offset:02x}': 0 for offset in validate_backend.OFFSETS}
        auto_status = {'mode': 'auto', 'requested_mode': 'auto'}
        with patch.object(validate_backend, 'get_main_pid', return_value=1234), \
                patch.object(validate_backend, 'request', return_value=auto_status), \
                patch.object(validate_backend, 'read_registers', return_value=baseline), \
                patch.object(validate_backend.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'fresh PID'):
                validate_backend.wait_crash_recovery(pre_crash_pid=1234, expected=baseline, timeout=0.1)

    def test_wait_crash_recovery_rejects_divergent_registers(self):
        baseline = {f'0x{offset:02x}': 0 for offset in validate_backend.OFFSETS}
        divergent = {**baseline, '0x22': 0x0c}
        auto_status = {'mode': 'auto', 'requested_mode': 'auto', 'temps': {'cpu': 50},
                       'fans': {'fan1': {'rpm': 2500}, 'fan2': {'rpm': 2500}}}
        with patch.object(validate_backend, 'get_main_pid', return_value=5678), \
                patch.object(validate_backend, 'request', return_value=auto_status), \
                patch.object(validate_backend, 'read_registers', return_value=divergent), \
                patch.object(validate_backend.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'baseline bytes'):
                validate_backend.wait_crash_recovery(pre_crash_pid=1234, expected=baseline, timeout=0.1)

    def test_wait_crash_recovery_rejects_non_auto_mode(self):
        baseline = {f'0x{offset:02x}': 0 for offset in validate_backend.OFFSETS}
        manual_status = {'mode': 'max', 'requested_mode': 'max'}
        with patch.object(validate_backend, 'get_main_pid', return_value=5678), \
                patch.object(validate_backend, 'request', return_value=manual_status), \
                patch.object(validate_backend, 'read_registers', return_value=baseline), \
                patch.object(validate_backend.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'firmware Auto'):
                validate_backend.wait_crash_recovery(pre_crash_pid=1234, expected=baseline, timeout=0.1)

    def test_wait_crash_recovery_succeeds_with_fresh_pid_and_baseline(self):
        baseline = {f'0x{offset:02x}': 0 for offset in validate_backend.OFFSETS}
        auto_status = {'mode': 'auto', 'requested_mode': 'auto', 'temps': {'cpu': 50},
                       'fans': {'fan1': {'rpm': 2500}, 'fan2': {'rpm': 2500}}}
        with patch.object(validate_backend, 'get_main_pid', return_value=5678), \
                patch.object(validate_backend, 'request', return_value=auto_status), \
                patch.object(validate_backend, 'read_registers', return_value=baseline):
            new_pid, status, registers = validate_backend.wait_crash_recovery(
                pre_crash_pid=1234, expected=baseline, timeout=1.0)
            self.assertEqual(new_pid, 5678)
            self.assertEqual(status['mode'], 'auto')
            self.assertEqual(registers, baseline)

    def test_wait_crash_recovery_rejects_failed_or_unhealthy_auto(self):
        baseline = {f'0x{offset:02x}': 0 for offset in validate_backend.OFFSETS}
        healthy = {'mode': 'auto', 'requested_mode': 'auto', 'temps': {'cpu': 50},
                   'fans': {'fan1': {'rpm': 2500}, 'fan2': {'rpm': 2500}}}
        for status in ({**healthy, 'last_error': 'NBFC child exited'},
                       {**healthy, 'emergency': True},
                       {**healthy, 'fans': {'fan1': {'rpm': None}, 'fan2': {'rpm': 2500}}}):
            with self.subTest(status=status), \
                    patch.object(validate_backend, 'get_main_pid', return_value=5678), \
                    patch.object(validate_backend, 'request', return_value=status), \
                    patch.object(validate_backend, 'read_registers', return_value=baseline), \
                    patch.object(validate_backend.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'fresh PID'):
                    validate_backend.wait_crash_recovery(1234, baseline, timeout=0.02)

    def test_crash_only_sets_reduced_stop_timer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = {f'0x{offset:02x}': 0 for offset in validate_backend.OFFSETS}
            policy = root / 'policy.json'
            import json
            policy.write_text(json.dumps({'expected_baselines': baseline}))
            config = root / 'config.toml'
            config.write_text(f'[nbfc]\npolicy_path = "{policy}"\n')
            support = root / 'write_support'
            support.write_text('N\n')
            status = {'temps': {'cpu': 50},
                      'fans': {'fan1': {'rpm': 2500}, 'fan2': {'rpm': 2500}},
                      'mode': 'auto', 'requested_mode': 'auto'}
            calls = []

            def service_call(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 3 if 'is-active' in command else 0, stdout='1000\n')

            with patch.object(validate_backend, 'CONFIG', config), \
                    patch.object(validate_backend, 'WRITE_SUPPORT', support), \
                    patch.object(validate_backend.subprocess, 'run', side_effect=service_call), \
                    patch.object(validate_backend, 'wait_auto', return_value=status), \
                    patch.object(validate_backend, 'read_registers', return_value=baseline), \
                    patch.object(validate_backend, 'request', return_value=status), \
                    patch.object(validate_backend, 'sample'), \
                    patch.object(validate_backend, 'wait_crash_recovery', return_value=(2000, status, baseline)), \
                    patch.object(validate_backend, 'record'):
                validate_backend.validate(seconds=10, crash_only=True)

            timer_calls = [cmd for cmd in calls if cmd[0] == 'systemd-run']
            self.assertEqual(len(timer_calls), 1)
            # 2 * 10 + 90 = 110s for crash_only vs 5 * 10 + 90 = 140s for full
            self.assertIn('--on-active=110s', timer_calls[0])


if __name__ == '__main__':
    unittest.main()
