"""Sleep validation must not mistake preparation, failed sleep or reboot for resume."""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import validate_suspend_resume as validation


class FakeRunner(validation.SystemRunner):
    def __init__(self):
        self.commands = []
        self.baseline = {f'0x{offset:02x}': 0 for offset in validation.validate_backend.OFFSETS}
        self.baseline['0x03'] = 33
        self.registers = dict(self.baseline)
        self.files = {
            validation.validate_backend.CONFIG: '[nbfc]\npolicy_path="/fake-policy.json"',
            Path('/fake-policy.json'): json.dumps({'expected_baselines': self.baseline}),
            validation.BOOT_ID_PATH: 'current-boot',
            validation.validate_backend.WRITE_SUPPORT: 'N',
        }
        self.active = False
        self.clock = 1000.0
        self.asleep = 0.0
        self.current_identity = {'pid': 1234, 'invocation': 'original'}
        self.hook = False
        self.fail_manual = False
        self.ignore_manual = False
        self.fail_stop = False
        self.failed_service = False
        self.unit_unloaded = False
        self.fail_recovery = False
        self.fail_timer = False
        self.locked = False
        self.journal = ''
        self.status = {'ok': True, 'mode': 'auto', 'requested_mode': 'auto',
                       'temps': {'cpu': 50}, 'last_error': None, 'emergency': False,
                       'fans': {'fan1': {'rpm': 2500, 'percent': None},
                                'fan2': {'rpm': 2400, 'percent': None}}}

    def run(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == 'systemd-run' and self.fail_timer:
            raise subprocess.CalledProcessError(1, command)
        if command[:3] == ['systemctl', 'show', '--property=ActiveState']:
            state = 'failed' if self.failed_service else 'active' if self.active else 'inactive'
            return subprocess.CompletedProcess(command, 0, stdout=state)
        if command == ['systemctl', 'start', validation.MAIN_UNIT]:
            self.active = True
            self.files[validation.validate_backend.WRITE_SUPPORT] = 'Y'
        if command == ['systemctl', 'stop', validation.MAIN_UNIT]:
            if self.fail_stop:
                raise subprocess.CalledProcessError(1, command)
            self.active = False
            self.registers = dict(self.baseline)
            self.files[validation.validate_backend.WRITE_SUPPORT] = 'N'
        if command == [sys.executable, str(validation.HARDWARE_TOOL), 'stop']:
            if self.fail_recovery:
                raise subprocess.CalledProcessError(1, command)
            self.registers = dict(self.baseline)
            self.files[validation.validate_backend.WRITE_SUPPORT] = 'N'
        if command == ['systemctl', 'reset-failed', validation.MAIN_UNIT]:
            if self.unit_unloaded:
                raise subprocess.CalledProcessError(1, command, stderr='Unit nitrofan.service not loaded')
            self.failed_service = False
        if command[0] == 'journalctl':
            return subprocess.CompletedProcess(command, 0, stdout=self.journal)
        if command[:2] == ['systemctl', 'suspend']:
            if self.locked:
                raise AssertionError('Suspend must release lock needed by the observer')
        return subprocess.CompletedProcess(command, 0, stdout='')

    def request(self, command):
        if command['cmd'] == 'set_mode':
            if self.fail_manual:
                raise RuntimeError('lost API')
            if not self.ignore_manual:
                self.status['mode'] = self.status['requested_mode'] = command['mode']
                for fan in self.status['fans'].values():
                    fan['percent'] = command['percent']
                self.registers.update({'0x37': command['percent'], '0x3a': command['percent'],
                                       '0x22': 0x0c, '0x21': 0x30})
        return json.loads(json.dumps(self.status))

    def read_registers(self):
        return dict(self.registers)

    def read_text(self, path):
        return self.files[path]

    def path_exists(self, path):
        return path in self.files or (path == validation.RUNTIME_LINK and self.hook)

    def unlink_file(self, path):
        self.files.pop(path, None)

    def create_runtime_hook(self):
        self.hook = True

    def remove_runtime_hook(self):
        self.hook = False

    def save(self, path, value):
        self.files[path] = json.dumps(value)

    def load(self):
        return json.loads(self.files[validation.STATE_PATH])

    def identity(self):
        return dict(self.current_identity)

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def suspended_seconds(self):
        return self.asleep

    @contextmanager
    def session_lock(self):
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def simulate_sleep(self, scenario='active'):
        self.active = False
        self.registers = dict(self.baseline)
        self.files[validation.validate_backend.WRITE_SUPPORT] = 'N'
        if scenario == 'active':
            self.files[validation.SLEEP_MARKER] = 'restart'
        validation.record_pre_sleep(self)
        self.asleep += 5
        self.clock += 1
        self.journal = json.dumps({'_BOOT_ID': 'current-boot',
                                  '__MONOTONIC_TIMESTAMP': str(int(self.clock * 1e6)),
                                  'MESSAGE': "System returned from sleep operation 'suspend'."})
        self.files.pop(validation.SLEEP_MARKER, None)
        if scenario == 'active':
            self.active = True
            self.files[validation.validate_backend.WRITE_SUPPORT] = 'Y'
            self.current_identity = {'pid': 5678, 'invocation': 'new'}
            self.status['mode'] = self.status['requested_mode'] = 'auto'
            for fan in self.status['fans'].values():
                fan['percent'] = None


class TestSuspendValidation(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.record = patch.object(validation, 'record').start()
        self.addCleanup(patch.stopall)

    def prepare(self, scenario='active'):
        return validation.prepare_validation(scenario, 60, self.runner)

    def test_prepare_only_arms_test_without_suspending(self):
        self.assertEqual(self.prepare(), 0)
        self.assertEqual(self.runner.load()['phase'], 'prepared')
        self.assertTrue(self.runner.hook)
        self.assertTrue(self.runner.active)
        self.assertNotIn(['systemctl', 'suspend'], self.runner.commands)
        self.assertTrue(any('--on-active=900s' in command for command in self.runner.commands))

    def test_prepare_rejects_running_service_before_mutation(self):
        self.runner.active = True
        with self.assertRaisesRegex(RuntimeError, 'cleanly stopped'):
            self.prepare()
        self.assertFalse(self.runner.hook)
        self.assertNotIn(validation.STATE_PATH, self.runner.files)

    def test_prepare_rejects_previous_checkpoint(self):
        self.prepare('stopped')
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            self.prepare()

    def test_failed_manual_command_cleans_up_started_service(self):
        self.runner.fail_manual = True
        with self.assertRaisesRegex(RuntimeError, 'lost API'):
            self.prepare()
        self.assertFalse(self.runner.active)
        self.assertFalse(self.runner.hook)
        self.assertNotIn(validation.STATE_PATH, self.runner.files)
        self.assertEqual(json.loads(self.runner.files[validation.RESULT_PATH])['outcome'], 'prepare_failed')

    def test_ignored_manual_command_cannot_pass_preparation(self):
        self.runner.ignore_manual = True
        with self.assertRaisesRegex(RuntimeError, 'Manual target'):
            self.prepare()
        self.assertFalse(self.runner.active)

    def test_failed_timer_never_starts_service(self):
        self.runner.fail_timer = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.prepare()
        self.assertFalse(self.runner.active)
        self.assertFalse(self.runner.hook)
        self.assertNotIn(validation.STATE_PATH, self.runner.files)

    def test_observer_requires_service_stopped_and_write_protection(self):
        self.prepare()
        with self.assertRaisesRegex(RuntimeError, 'cleanly stopped'):
            validation.record_pre_sleep(self.runner)
        self.runner.active = False
        self.runner.registers = dict(self.runner.baseline)
        with self.assertRaisesRegex(RuntimeError, 'write protection'):
            validation.record_pre_sleep(self.runner)

    def test_observer_requires_active_restart_marker(self):
        self.prepare()
        self.runner.active = False
        self.runner.registers = dict(self.runner.baseline)
        self.runner.files[validation.validate_backend.WRITE_SUPPORT] = 'N'
        with self.assertRaisesRegex(RuntimeError, 'restart intent'):
            validation.record_pre_sleep(self.runner)

    def test_no_sleep_is_inconclusive_and_preserves_timer_and_checkpoint(self):
        self.prepare()
        self.assertEqual(validation.verify_validation(self.runner), 2)
        self.assertIn(validation.STATE_PATH, self.runner.files)
        self.assertNotIn(['systemctl', 'stop', f'{validation.TIMER_UNIT}.timer'], self.runner.commands)

    def test_hook_logs_and_stale_target_timestamp_do_not_prove_sleep(self):
        self.prepare()
        self.runner.journal = 'Release NitroFan control during sleep\nReached target Sleep'
        self.assertFalse(validation.prove_sleep_occurred(self.runner.load(), self.runner)[0])

    def test_old_resume_journal_is_rejected_even_with_clock_gap(self):
        self.prepare()
        self.runner.simulate_sleep()
        entry = json.loads(self.runner.journal)
        entry['__MONOTONIC_TIMESTAMP'] = '100'
        self.runner.journal = json.dumps(entry)
        self.assertFalse(validation.prove_sleep_occurred(self.runner.load(), self.runner)[0])

    def test_journal_from_another_boot_is_rejected(self):
        self.prepare()
        self.runner.simulate_sleep()
        entry = json.loads(self.runner.journal)
        entry['_BOOT_ID'] = 'previous-boot'
        self.runner.journal = json.dumps(entry)
        self.assertFalse(validation.prove_sleep_occurred(self.runner.load(), self.runner)[0])

    def test_proc_and_journal_boot_uuid_formats_match(self):
        self.runner.files[validation.BOOT_ID_PATH] = '12345678-1234-5678-1234-567812345678'
        self.prepare()
        self.runner.simulate_sleep()
        entry = json.loads(self.runner.journal)
        entry['_BOOT_ID'] = '12345678123456781234567812345678'
        self.runner.journal = json.dumps(entry)
        self.assertTrue(validation.prove_sleep_occurred(self.runner.load(), self.runner)[0])

    def test_reboot_is_not_suspend_resume(self):
        self.prepare()
        self.runner.files[validation.BOOT_ID_PATH] = 'new-boot'
        with self.assertRaisesRegex(RuntimeError, 'rebooted'):
            validation.verify_validation(self.runner)

    def test_no_pre_sleep_evidence_cannot_pass(self):
        self.prepare()
        self.runner.simulate_sleep()
        checkpoint = self.runner.load()
        checkpoint.pop('pre_sleep')
        self.runner.save(validation.STATE_PATH, checkpoint)
        with self.assertRaisesRegex(RuntimeError, 'pre-sleep firmware handoff'):
            validation.verify_validation(self.runner)
        self.assertFalse(self.runner.active)

    def test_active_cycle_passes_only_after_handoff_resume_and_cleanup(self):
        self.prepare()
        self.runner.simulate_sleep()
        self.assertEqual(validation.verify_validation(self.runner), 0)
        self.assertFalse(self.runner.active)
        self.assertFalse(self.runner.hook)
        self.assertNotIn(validation.STATE_PATH, self.runner.files)
        result = json.loads(self.runner.files[validation.RESULT_PATH])
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(result['pre_sleep']['write_support'], 'N')
        self.assertIn('kernel suspend time', result['sleep_proof'])

    def test_pid_reuse_with_fresh_systemd_invocation_is_accepted(self):
        self.prepare()
        self.runner.simulate_sleep()
        self.runner.current_identity['pid'] = 1234
        self.assertEqual(validation.verify_validation(self.runner), 0)

    def test_same_invocation_after_wake_fails_and_stops_service(self):
        self.prepare()
        self.runner.simulate_sleep()
        self.runner.current_identity['invocation'] = 'original'
        with self.assertRaisesRegex(RuntimeError, 'fresh daemon invocation'):
            validation.verify_validation(self.runner)
        self.assertFalse(self.runner.active)

    def test_missing_fan_after_wake_fails(self):
        self.prepare()
        self.runner.simulate_sleep()
        self.runner.status['fans']['fan1']['rpm'] = None
        with self.assertRaisesRegex(RuntimeError, 'healthy telemetry'):
            validation.verify_validation(self.runner)
        self.assertFalse(self.runner.active)

    def test_manual_mode_after_wake_fails(self):
        self.prepare()
        self.runner.simulate_sleep()
        self.runner.status['mode'] = self.runner.status['requested_mode'] = 'fixed'
        with self.assertRaisesRegex(RuntimeError, 'firmware Auto'):
            validation.verify_validation(self.runner)

    def test_divergent_registers_after_wake_fail(self):
        self.prepare()
        self.runner.simulate_sleep()
        self.runner.registers['0x37'] = 60
        with self.assertRaisesRegex(RuntimeError, 'baseline registers'):
            validation.verify_validation(self.runner)

    def test_stopped_cycle_never_starts_service(self):
        self.prepare('stopped')
        self.runner.simulate_sleep('stopped')
        self.assertEqual(validation.verify_validation(self.runner), 0)
        self.assertNotIn(['systemctl', 'start', validation.MAIN_UNIT], self.runner.commands)

    def test_unexpected_start_in_stopped_cycle_is_stopped_during_cleanup(self):
        self.prepare('stopped')
        self.runner.simulate_sleep('stopped')
        self.runner.active = True
        with self.assertRaisesRegex(RuntimeError, 'cleanly stopped'):
            validation.verify_validation(self.runner)
        self.assertFalse(self.runner.active)

    def test_failed_cleanup_preserves_checkpoint_and_safety_timer(self):
        self.prepare()
        self.runner.simulate_sleep()
        self.runner.fail_stop = True
        with self.assertRaises(subprocess.CalledProcessError):
            validation.verify_validation(self.runner)
        self.assertIn(validation.STATE_PATH, self.runner.files)
        self.assertNotIn(['systemctl', 'stop', f'{validation.TIMER_UNIT}.timer'], self.runner.commands)

    def test_cancel_without_sleep_restores_safe_stopped_state(self):
        self.prepare()
        self.assertEqual(validation.dispatch(argparse.Namespace(command='cancel'), self.runner), 0)
        self.assertFalse(self.runner.active)
        self.assertFalse(self.runner.hook)

    def test_cancel_retries_handoff_after_failed_service_stop(self):
        self.prepare()
        self.runner.active = False
        self.runner.failed_service = True
        self.runner.files[validation.validate_backend.WRITE_SUPPORT] = 'Y'
        self.runner.files[validation.SLEEP_MARKER] = 'restart'
        self.assertEqual(validation.dispatch(argparse.Namespace(command='cancel'), self.runner), 0)
        self.assertFalse(self.runner.failed_service)
        self.assertEqual(self.runner.files[validation.validate_backend.WRITE_SUPPORT], 'N')
        self.assertNotIn(validation.STATE_PATH, self.runner.files)
        self.assertNotIn(validation.SLEEP_MARKER, self.runner.files)
        recovery = [sys.executable, str(validation.HARDWARE_TOOL), 'stop']
        reset = ['systemctl', 'reset-failed', validation.MAIN_UNIT]
        self.assertLess(self.runner.commands.index(recovery), self.runner.commands.index(reset))

    def test_failed_recovery_during_cancel_keeps_checkpoint_and_timer(self):
        self.prepare()
        self.runner.failed_service = True
        self.runner.fail_recovery = True
        self.runner.files[validation.SLEEP_MARKER] = 'restart'
        with self.assertRaises(subprocess.CalledProcessError):
            validation.dispatch(argparse.Namespace(command='cancel'), self.runner)
        self.assertTrue(self.runner.failed_service)
        self.assertIn(validation.STATE_PATH, self.runner.files)
        self.assertNotIn(['systemctl', 'stop', f'{validation.TIMER_UNIT}.timer'], self.runner.commands)
        self.assertTrue(self.runner.hook)
        self.assertIn(validation.SLEEP_MARKER, self.runner.files)

    def test_cancel_without_checkpoint_clears_stale_intent_after_verified_release(self):
        self.runner.failed_service = True
        self.runner.files[validation.SLEEP_MARKER] = 'restart'
        self.runner.files[validation.validate_backend.WRITE_SUPPORT] = 'Y'
        self.assertEqual(validation.dispatch(argparse.Namespace(command='cancel'), self.runner), 0)
        self.assertFalse(self.runner.failed_service)
        self.assertNotIn(validation.SLEEP_MARKER, self.runner.files)
        self.assertEqual(self.runner.files[validation.validate_backend.WRITE_SUPPORT], 'N')

    def test_cancel_skips_reset_of_an_inactive_unloaded_unit(self):
        self.runner.unit_unloaded = True
        self.runner.files[validation.SLEEP_MARKER] = 'restart'
        self.assertEqual(validation.dispatch(argparse.Namespace(command='cancel'), self.runner), 0)
        self.assertNotIn(['systemctl', 'reset-failed', validation.MAIN_UNIT], self.runner.commands)
        self.assertNotIn(validation.SLEEP_MARKER, self.runner.files)
        result = json.loads(self.runner.files[validation.RESULT_PATH])
        self.assertEqual(result['cleanup']['write_support'], 'N')

    def test_suspend_command_releases_lock_for_pre_sleep_observer(self):
        self.prepare('stopped')
        with patch.object(validation, 'SystemRunner', return_value=self.runner), \
                patch.object(validation.os, 'geteuid', return_value=0), \
                patch.object(validation.sys, 'platform', 'linux'), \
                patch.object(validation.sys, 'argv', ['validator', 'suspend']):
            self.assertEqual(validation.main(), 0)
        self.assertIn(['systemctl', 'suspend'], self.runner.commands)


class TestPrivateCheckpoints(unittest.TestCase):
    def test_atomic_private_save_and_symlink_read_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / 'suspend.json'
            runner = validation.SystemRunner()
            with patch.object(validation, 'STATE_PATH', path), \
                    patch.object(runner, '_check_state_directory'):
                runner.save(path, {'version': 1})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(runner.load(), {'version': 1})
                other = directory / 'other.json'
                path.rename(other)
                path.symlink_to(other)
                with self.assertRaises(OSError):
                    runner.load()

    def test_world_readable_checkpoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'suspend.json'
            path.write_text('{"version":1}')
            path.chmod(0o644)
            runner = validation.SystemRunner()
            with patch.object(validation, 'STATE_PATH', path), \
                    patch.object(runner, '_check_state_directory'):
                with self.assertRaisesRegex(RuntimeError, 'ownership, type or size'):
                    runner.load()

    def test_lock_rejects_concurrent_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'suspend.json'
            runner = validation.SystemRunner()
            with patch.object(validation, 'STATE_PATH', path), \
                    patch.object(runner, '_check_state_directory'):
                with runner.session_lock():
                    with self.assertRaises(BlockingIOError):
                        with runner.session_lock():
                            self.fail('Second lock unexpectedly acquired')


if __name__ == '__main__':
    unittest.main()
