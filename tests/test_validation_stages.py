"""Thermal interruptions defer low speeds without masking control or sensor failures."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import validate_backend as validation


def status(cpu=50, emergency=False):
    return {'ok': True, 'mode': 'auto', 'requested_mode': 'auto', 'emergency': emergency,
            'temps': {'cpu': cpu}, 'last_error': None,
            'fans': {'fan1': {'rpm': 2500, 'percent': None},
                     'fan2': {'rpm': 2400, 'percent': None}}}


class TestStageReadings(unittest.TestCase):
    def test_hot_max_is_allowed_but_lower_speeds_are_deferred(self):
        reading = status(92, emergency=True)
        validation.check_readings(reading, 10, max_stage=True)
        with self.assertRaises(validation.ThermalHold):
            validation.check_readings(reading, 10)

    def test_max_still_aborts_critical_invalid_or_missing_cpu(self):
        for value in (100, 110, None, True, float('nan'), float('inf')):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError) as raised:
                    validation.check_readings(status(value), 10, max_stage=True)
                self.assertNotIsInstance(raised.exception, validation.ThermalHold)

    def test_sensor_and_backend_faults_are_never_reported_as_thermal_deferrals(self):
        fan_fault = status(83)
        fan_fault['fans']['fan1']['rpm'] = 0
        backend_fault = status(83)
        backend_fault['last_error'] = 'lost controller'
        for reading in (fan_fault, backend_fault):
            with self.assertRaises(RuntimeError) as raised:
                validation.check_readings(reading, 10)
            self.assertNotIsInstance(raised.exception, validation.ThermalHold)

    def test_guardian_temperature_also_bounds_validation(self):
        reading = status(50)
        reading['safety'] = {'temps': {'cpu': 92}}
        with self.assertRaises(validation.ThermalHold):
            validation.check_readings(reading, 10)
        reading['safety']['temps']['cpu'] = 100
        with self.assertRaisesRegex(RuntimeError, 'abort limit'):
            validation.check_readings(reading, 10, max_stage=True)

    def test_max_sampling_keeps_guard_active_and_checks_actual_target(self):
        reading = status(92, emergency=True)
        reading['mode'] = 'emergency'
        reading['requested_mode'] = 'max'
        for fan in reading['fans'].values():
            fan['percent'] = 100
        registers = {'0x37': 100, '0x3a': 100, '0x22': 12, '0x21': 48}
        with patch.object(validation, 'request', return_value=reading), \
                patch.object(validation, 'read_registers', return_value=registers), \
                patch.object(validation.time, 'monotonic', side_effect=[0, 6, 11]), \
                patch.object(validation.time, 'sleep'), patch.object(validation, 'record'):
            validation.sample('max', 10, 100, {})


class TestValidationStages(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.baseline = {f'0x{offset:02x}': 0 for offset in validation.OFFSETS}
        policy = root / 'policy.json'
        policy.write_text(json.dumps({'expected_baselines': self.baseline}))
        configuration = root / 'config.toml'
        configuration.write_text(f'[nbfc]\npolicy_path="{policy}"\n')
        support = root / 'write_support'
        support.write_text('N')
        self.commands = []

        def service_call(command, **kwargs):
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 3 if 'is-active' in command else 0)

        def mocked(name, **kwargs):
            mock = patch.object(validation, name, **kwargs)
            self.addCleanup(mock.stop)
            return mock.start()

        mocked('CONFIG', new=configuration)
        mocked('WRITE_SUPPORT', new=support)
        mock = patch.object(validation.subprocess, 'run', side_effect=service_call)
        self.addCleanup(mock.stop)
        mock.start()
        mocked('read_registers', return_value=self.baseline)
        self.start = mocked('wait_auto', return_value=status())
        self.request = mocked('request', return_value=status())
        self.sample = mocked('sample')
        mocked('get_main_pid', return_value=1000)
        self.recovery = mocked('wait_crash_recovery', return_value=(2000, status(), self.baseline))
        self.record = mocked('record')

    def test_all_cool_stages_complete(self):
        self.assertTrue(validation.validate(10))
        self.assertEqual([call.args[0] for call in self.sample.call_args_list],
                         ['manual-25', 'manual-30', 'manual-60', 'max', 'firmware-auto'])
        self.assertEqual(self.record.call_args.args[0], 'passed')

    def test_low_speed_thermal_hold_continues_to_max_and_crash(self):
        def sample(stage, *args):
            if stage == 'manual-25':
                raise validation.ThermalHold('CPU 83 C', status(83))

        self.sample.side_effect = sample
        self.assertFalse(validation.validate(10))
        self.recovery.assert_called_once()
        result = self.record.call_args
        self.assertEqual(result.args[0], 'partial')
        self.assertEqual(result.kwargs['pending'], ['manual-25', 'manual-30', 'manual-60'])
        self.assertIn('crash_recovery', result.kwargs['completed'])
        self.assertIn('shutdown_firmware_handoff', result.kwargs['completed'])
        self.assertNotIn('manual-25', result.kwargs['completed'])

    def test_warm_precheck_never_requests_low_fan_speed(self):
        self.start.return_value = self.request.return_value = status(85)
        self.assertFalse(validation.validate(10))
        for call in self.request.call_args_list:
            self.assertNotEqual(call.args[0].get('mode'), 'fixed')
        self.recovery.assert_called_once()

    def test_auto_thermal_hold_does_not_erase_crash_result(self):
        def sample(stage, *args):
            if stage == 'firmware-auto':
                raise validation.ThermalHold('CPU 83 C', status(83))

        self.sample.side_effect = sample
        self.assertFalse(validation.validate(10, crash_only=True))
        self.assertEqual(self.record.call_args.kwargs['pending'], ['extended_auto_observation'])
        self.assertIn('crash_recovery', self.record.call_args.kwargs['completed'])

    def test_nonthermal_failure_stops_without_claiming_crash_pass(self):
        self.sample.side_effect = RuntimeError('fan stopped')
        with self.assertRaisesRegex(RuntimeError, 'fan stopped'):
            validation.validate(10)
        self.recovery.assert_not_called()
        self.assertIn(['systemctl', 'stop', 'nitrofan.service'], self.commands)
        self.assertNotIn('passed', [call.args[0] for call in self.record.call_args_list])


if __name__ == '__main__':
    unittest.main()
