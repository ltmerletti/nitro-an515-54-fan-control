#!/usr/bin/env python3
"""Prepare a bounded sleep test, record handoff before sleep, and verify wake.

Only the explicit suspend subcommand sleeps the laptop. Preparation requires a
stopped service. Both scenarios finish stopped; boot startup is never enabled.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib

try:
    from tools import validate_backend
except ModuleNotFoundError:
    import validate_backend

MAIN_UNIT = 'nitrofan.service'
CHECK_UNIT = 'nitrofan-suspend-check.service'
TIMER_UNIT = 'nitrofan-suspend-stop'
RUNTIME_LINK = Path('/run/systemd/system/sleep.target.requires') / CHECK_UNIT
CHECK_UNIT_PATH = Path('/etc/systemd/system') / CHECK_UNIT
STATE_PATH = Path('/var/lib/nitrofan-validation/suspend.json')
RESULT_PATH = STATE_PATH.with_name('last-suspend-result.json')
SLEEP_MARKER = Path('/run/nitrofan-resume')
HARDWARE_TOOL = Path(__file__).with_name('service_hardware.py')
BOOT_ID_PATH = Path('/proc/sys/kernel/random/boot_id')
SAFETY_SECONDS = 900
MIN_SLEEP_SECONDS = 2.0


class SystemRunner:
    """System operations are injectable so tests never touch the laptop."""

    def run(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        kwargs.setdefault('timeout', 15)
        return subprocess.run(command, **kwargs)

    request = staticmethod(validate_backend.request)
    read_registers = staticmethod(validate_backend.read_registers)

    def read_text(self, path: Path) -> str:
        return path.read_text()

    def path_exists(self, path: Path) -> bool:
        return path.exists() or path.is_symlink()

    def unlink_file(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def create_runtime_hook(self) -> None:
        if not CHECK_UNIT_PATH.is_file():
            raise RuntimeError('Install the suspend validation unit before preparing this test')
        RUNTIME_LINK.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_LINK.symlink_to(CHECK_UNIT_PATH)
        self.run(['systemctl', 'daemon-reload'], check=True)

    def remove_runtime_hook(self) -> None:
        if self.path_exists(RUNTIME_LINK):
            if not RUNTIME_LINK.is_symlink() or RUNTIME_LINK.readlink() != CHECK_UNIT_PATH:
                raise RuntimeError('Runtime validation hook was replaced; leaving it untouched')
            RUNTIME_LINK.unlink()
            self.run(['systemctl', 'daemon-reload'], check=True)

    def identity(self) -> dict:
        result = self.run(['systemctl', 'show', '--property=MainPID,InvocationID', MAIN_UNIT],
                          capture_output=True, text=True, check=True)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        return {'pid': int(values.get('MainPID', '0')),
                'invocation': values.get('InvocationID', '')}

    def suspended_seconds(self) -> float:
        # CLOCK_MONOTONIC stops in suspend; CLOCK_BOOTTIME includes suspend.
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()

    sleep = staticmethod(time.sleep)
    monotonic = staticmethod(time.monotonic)

    def _check_state_directory(self) -> None:
        directory = STATE_PATH.parent
        for parent in reversed(directory.parents):
            metadata = parent.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o022):
                raise RuntimeError(f'Untrusted validation state parent: {parent}')
        directory.mkdir(mode=0o700, exist_ok=True)
        metadata = directory.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077):
            raise RuntimeError(f'Validation state directory must be private: {directory}')

    def save(self, path: Path, value: dict) -> None:
        self._check_state_directory()
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.suspend-')
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(value, stream, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def load(self) -> dict:
        self._check_state_directory()
        descriptor = os.open(STATE_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor) as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o077 or metadata.st_size > 65536):
                raise RuntimeError('Invalid suspend validation checkpoint ownership, type or size')
            result = json.load(stream)
        if not isinstance(result, dict) or result.get('version') != 1:
            raise RuntimeError('Invalid suspend validation checkpoint')
        return result

    @contextmanager
    def session_lock(self):
        self._check_state_directory()
        descriptor = os.open(STATE_PATH.parent / 'validation.lock',
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o077):
                raise RuntimeError('Untrusted validation lock file')
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)


def record(event: str, **fields) -> None:
    validate_backend.record(event, **fields)


def expected_baseline(runner: SystemRunner) -> dict:
    config = tomllib.loads(runner.read_text(validate_backend.CONFIG))
    policy_path = Path(config['nbfc'].get(
        'policy_path', '/opt/nitrofan/tools/ec_guard/config/ec_guard_control_policy.json'))
    policy = json.loads(runner.read_text(policy_path))
    return {f'0x{int(offset, 16):02x}': value
            for offset, value in policy['expected_baselines'].items()}


def service_stopped(runner: SystemRunner) -> bool:
    result = runner.run(['systemctl', 'show', '--property=ActiveState', '--value', MAIN_UNIT],
                        capture_output=True, text=True, check=True)
    return result.stdout.strip() == 'inactive'


def check_released(runner: SystemRunner, expected: dict) -> dict:
    if not service_stopped(runner):
        raise RuntimeError('Service is not cleanly stopped')
    registers = runner.read_registers()
    support = runner.read_text(validate_backend.WRITE_SUPPORT).strip()
    if registers != expected or support not in ('N', '0'):
        raise RuntimeError('Firmware baseline and EC write protection were not restored')
    return {'registers': registers, 'write_support': support}


def check_boot(runner: SystemRunner, checkpoint: dict) -> None:
    if runner.read_text(BOOT_ID_PATH).strip() != checkpoint['boot_id']:
        raise RuntimeError('Machine rebooted; this does not validate suspend/resume. Cancel the test.')


def wait_auto(runner: SystemRunner) -> dict:
    deadline = runner.monotonic() + 30
    while runner.monotonic() < deadline:
        try:
            status = runner.request({'cmd': 'status'})
            if status.get('mode') == status.get('requested_mode') == 'auto':
                validate_backend.check_readings(status, 5)
                return status
        except (OSError, ValueError, RuntimeError):
            pass
        runner.sleep(0.25)
    raise RuntimeError('Service did not return with healthy telemetry in firmware Auto')


def cleanup(runner: SystemRunner, checkpoint: dict, outcome: str) -> None:
    previous = {signum: signal.signal(signum, signal.SIG_IGN)
                for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        # An aborted sleep can leave the wake hook queued. Finish it before the
        # final main-service stop so it cannot restart control after cleanup.
        runner.run(['systemctl', 'stop', CHECK_UNIT, 'nitrofan-sleep.service'], check=True, timeout=45)
        runner.run(['systemctl', 'stop', MAIN_UNIT], check=True, timeout=40)
        # Stopping an already failed unit does not repeat ExecStopPost. Retry
        # guarded recovery and write protection before clearing its failure.
        runner.run([sys.executable, str(HARDWARE_TOOL), 'stop'], check=True, timeout=30)
        state = runner.run(['systemctl', 'show', '--property=ActiveState', '--value', MAIN_UNIT],
                           capture_output=True, text=True, check=True).stdout.strip()
        # Inactive units may have been garbage-collected by systemd. ResetFailed
        # does not load them, and clearing failure state is unnecessary for them.
        if state == 'failed':
            runner.run(['systemctl', 'reset-failed', MAIN_UNIT], check=True)
        released = check_released(runner, expected_baseline(runner))
        # A failed pre-sleep hook never reaches ExecStop, so its restart marker
        # may survive cancellation. Retire it only after verified release.
        runner.unlink_file(SLEEP_MARKER)
        # Do not remove recovery evidence or the independent timer until stop,
        # baseline verification and write protection have all succeeded.
        runner.remove_runtime_hook()
        for suffix in ('timer', 'service'):
            stopped = runner.run(['systemctl', 'stop', f'{TIMER_UNIT}.{suffix}'],
                                 check=False, capture_output=True, text=True)
            if stopped.returncode not in (0, 5):
                raise RuntimeError(f'Cannot stop validation safety {suffix}: {stopped.stderr}')
        checkpoint.update(outcome=outcome, cleanup=released)
        runner.save(RESULT_PATH, checkpoint)
        runner.unlink_file(STATE_PATH)
        record('cleanup', outcome=outcome, **released)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def prepare_validation(scenario: str, target_percent: int, runner: SystemRunner) -> int:
    if runner.path_exists(STATE_PATH) or runner.path_exists(RUNTIME_LINK):
        raise RuntimeError('A validation session already exists; verify or cancel it first')
    if runner.path_exists(SLEEP_MARKER):
        raise RuntimeError('An earlier sleep handoff has not finished')
    baseline = expected_baseline(runner)
    check_released(runner, baseline)
    checkpoint = {'version': 1, 'scenario': scenario, 'phase': 'preparing',
                  'boot_id': runner.read_text(BOOT_ID_PATH).strip(),
                  'baseline': baseline, 'target_percent': target_percent}
    runner.save(STATE_PATH, checkpoint)
    try:
        runner.run(['systemd-run', '--quiet', '--collect', f'--unit={TIMER_UNIT}',
                    f'--on-active={SAFETY_SECONDS}s', '/usr/bin/systemctl', 'stop', MAIN_UNIT],
                   check=True)
        checkpoint['timer_armed'] = True
        runner.save(STATE_PATH, checkpoint)
        runner.create_runtime_hook()
        if scenario == 'active':
            runner.run(['systemctl', 'start', MAIN_UNIT], check=True, timeout=40)
            wait_auto(runner)
            runner.request({'cmd': 'set_mode', 'mode': 'fixed', 'percent': target_percent})
            runner.sleep(2)
            status = runner.request({'cmd': 'status'})
            validate_backend.check_readings(status, 5)
            registers = runner.read_registers()
            if (any(registers[key] != target_percent for key in ('0x37', '0x3a'))
                    or registers['0x22'] != 0x0c or registers['0x21'] != 0x30
                    or status.get('mode') != 'fixed'
                    or any(status['fans'][fan]['percent'] != target_percent
                           for fan in ('fan1', 'fan2'))):
                raise RuntimeError('Pre-sleep Manual target was not applied to both fans')
            checkpoint['pre_identity'] = runner.identity()
            if (checkpoint['pre_identity']['pid'] <= 0
                    or not checkpoint['pre_identity']['invocation']):
                raise RuntimeError('Cannot determine pre-sleep daemon identity')
        checkpoint.update(phase='prepared', suspended_seconds=runner.suspended_seconds(),
                          prepare_iso=datetime.now(timezone.utc).isoformat(),
                          prepare_monotonic=runner.monotonic())
        runner.save(STATE_PATH, checkpoint)
    except BaseException:
        # If arming the timer failed, no service has been started yet. Otherwise
        # cleanup is mandatory even when startup, IPC or checkpoint writes fail.
        if checkpoint.get('timer_armed'):
            cleanup(runner, checkpoint, 'prepare_failed')
        else:
            runner.save(RESULT_PATH, {**checkpoint, 'outcome': 'prepare_failed'})
            runner.unlink_file(STATE_PATH)
        raise
    record('prepared', scenario=scenario, target_percent=target_percent,
           checkpoint=str(STATE_PATH), safety_seconds=SAFETY_SECONDS)
    return 0


def record_pre_sleep(runner: SystemRunner) -> int:
    """Called by the required observer unit after the real NitroFan sleep hook."""
    checkpoint = runner.load()
    check_boot(runner, checkpoint)
    if checkpoint['phase'] != 'prepared':
        raise RuntimeError('Validation checkpoint is not ready for sleep')
    evidence = check_released(runner, expected_baseline(runner))
    if runner.path_exists(SLEEP_MARKER) != (checkpoint['scenario'] == 'active'):
        raise RuntimeError('Sleep hook did not preserve the expected restart intent')
    checkpoint.update(phase='released', pre_sleep=evidence)
    runner.save(STATE_PATH, checkpoint)
    record('pre_sleep_released', **evidence)
    return 0


def prove_sleep_occurred(checkpoint: dict, runner: SystemRunner) -> tuple[bool, str]:
    check_boot(runner, checkpoint)
    elapsed_asleep = runner.suspended_seconds() - checkpoint['suspended_seconds']
    if elapsed_asleep < MIN_SLEEP_SECONDS:
        return False, 'Kernel clocks do not show at least two seconds suspended since preparation'
    result = runner.run(['journalctl', '-b', f'--since={checkpoint["prepare_iso"]}',
                         '-u', 'systemd-suspend.service', '-o', 'json', '--no-pager'],
                        capture_output=True, text=True, check=False)
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            try:
                entry = json.loads(line)
                recent = int(entry['__MONOTONIC_TIMESTAMP']) / 1e6 >= checkpoint['prepare_monotonic']
                if (recent and str(entry.get('_BOOT_ID', '')).replace('-', '')
                        == checkpoint['boot_id'].replace('-', '')
                        and str(entry.get('MESSAGE', '')).startswith('System returned from sleep')):
                    return True, f'Current-boot resume journal and {elapsed_asleep:.2f}s kernel suspend time'
            except (ValueError, TypeError, KeyError):
                continue
    return False, 'No matching completed suspend/resume journal record from this test'


def verify_validation(runner: SystemRunner) -> int:
    checkpoint = runner.load()
    proven, detail = prove_sleep_occurred(checkpoint, runner)
    if not proven:
        record('inconclusive', detail=detail, note='Sleep at least five seconds, then verify; or cancel')
        return 2
    try:
        if checkpoint['phase'] != 'released' or 'pre_sleep' not in checkpoint:
            raise RuntimeError('No verified pre-sleep firmware handoff was recorded')
        baseline = expected_baseline(runner)
        if checkpoint['baseline'] != baseline:
            raise RuntimeError('Configured EC baseline changed during validation')
        if runner.path_exists(SLEEP_MARKER):
            raise RuntimeError('Wake hook did not finish clearing its restart marker')
        if checkpoint['scenario'] == 'active':
            status = wait_auto(runner)
            identity = runner.identity()
            if (identity['pid'] <= 0 or not identity['invocation']
                    or identity['invocation'] == checkpoint['pre_identity']['invocation']):
                raise RuntimeError('No fresh daemon invocation after resume')
            if runner.read_registers() != baseline:
                raise RuntimeError('Resumed Auto mode did not restore baseline registers')
            checkpoint.update(post_identity=identity, post_status=status)
        else:
            checkpoint['post_stop'] = check_released(runner, baseline)
        checkpoint['sleep_proof'] = detail
    except BaseException as error:
        checkpoint['error'] = str(error)
        cleanup(runner, checkpoint, 'failed')
        raise
    cleanup(runner, checkpoint, 'passed')
    record('passed', scenario=checkpoint['scenario'], detail=detail)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='Arm test without sleeping the machine')
    prepare.add_argument('--scenario', choices=('active', 'stopped'), default='active')
    prepare.add_argument('--target-percent', type=int, default=60, choices=range(25, 101),
                         metavar='25..100')
    commands.add_parser('suspend', help='Explicitly sleep; operator must wake the laptop')
    commands.add_parser('verify', help='Check wake and finish safely')
    commands.add_parser('cancel', help='Stop service and remove this test after checked cleanup')
    commands.add_parser('pre-sleep', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.geteuid() != 0 or sys.platform != 'linux':
        parser.error('Run with sudo on the Linux laptop after installing the backend')

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupt)
    runner = SystemRunner()
    try:
        with runner.session_lock():
            if args.command != 'suspend':
                return dispatch(args, runner)
            checkpoint = runner.load()
            check_boot(runner, checkpoint)
            if checkpoint['phase'] != 'prepared':
                raise RuntimeError('Prepare a new test before suspending')
        # The pre-sleep observer needs this lock. Do not hold it while asking
        # systemd to run the sleep transaction.
        runner.run(['systemctl', 'suspend'], check=True, timeout=60)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        record('failed', error=str(error) or 'Interrupted')
        print(f'Suspend validation failed: {error}', file=sys.stderr)
        return 1


def dispatch(args: argparse.Namespace, runner: SystemRunner) -> int:
    if args.command == 'prepare':
        return prepare_validation(args.scenario, args.target_percent, runner)
    if args.command == 'verify':
        return verify_validation(runner)
    if args.command == 'pre-sleep':
        return record_pre_sleep(runner)
    checkpoint = runner.load() if runner.path_exists(STATE_PATH) else {
        'version': 1, 'scenario': 'unknown', 'phase': 'cancel_without_checkpoint',
    }
    cleanup(runner, checkpoint, 'cancelled')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
