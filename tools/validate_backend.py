#!/usr/bin/env python3
"""Bounded on-laptop check of Manual, Max, crash recovery and firmware Auto.

Starts the installed service, records telemetry as JSON lines, then stops the
service and checks restoration. Does not enable boot startup or load a workload.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import tomllib

SOCKET = '/run/nitrofan/nitrofan.sock'
EC_PATH = '/sys/kernel/debug/ec/ec0/io'
CONFIG = Path('/etc/nitrofan/config.toml')
WRITE_SUPPORT = Path('/sys/module/ec_sys/parameters/write_support')
OFFSETS = (0x03, 0x10, 0x21, 0x22, 0x37, 0x38, 0x3a, 0x3b)
LOW_SPEED_LIMIT_C = 80
MAX_STAGE_ABORT_C = 100  # AN515-54 coretemp reports a 100 C critical temperature.


class ThermalHold(RuntimeError):
    """Low-speed or extended Auto observation needs cooler conditions."""

    def __init__(self, message: str, status: dict):
        super().__init__(message)
        self.status = status


def request(command: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(15)
        client.connect(SOCKET)
        client.sendall(json.dumps(command).encode() + b'\n')
        with client.makefile('rb') as stream:
            data = stream.readline(65537)
    if len(data) > 65536 or not data.endswith(b'\n'):
        raise RuntimeError('Invalid service response frame')
    reply = json.loads(data)
    if reply.get('ok') is not True:
        raise RuntimeError(f'Service rejected request: {reply}')
    return reply


def read_registers() -> dict[str, int]:
    descriptor = os.open(EC_PATH, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        result = {}
        for offset in OFFSETS:
            value = os.pread(descriptor, 1, offset)
            if len(value) != 1:
                raise RuntimeError(f'Short EC read at {offset:#x}')
            result[f'0x{offset:02x}'] = value[0]
        return result
    finally:
        os.close(descriptor)


def record(event: str, **fields) -> None:
    print(json.dumps({'event': event, 'timestamp': time.time(), **fields}, allow_nan=False), flush=True)


def get_main_pid(unit: str = 'nitrofan.service') -> int:
    result = subprocess.run(['systemctl', 'show', '--property=MainPID', '--value', unit],
                            capture_output=True, text=True, check=False, timeout=10)
    if result.returncode != 0 or not result.stdout:
        return 0
    try:
        return int(result.stdout.strip())
    except (ValueError, TypeError):
        return 0


def wait_auto(timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = request({'cmd': 'status'})
            if status['mode'] == 'auto' and status['requested_mode'] == 'auto':
                return status
        except (OSError, RuntimeError, ValueError):
            pass
        time.sleep(0.25)
    raise RuntimeError('Service did not return in firmware Auto')


def wait_crash_recovery(pre_crash_pid: int, expected: dict[str, int], timeout: float = 45) -> tuple[int, dict, dict[str, int]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new_pid = get_main_pid('nitrofan.service')
        if new_pid > 0 and new_pid != pre_crash_pid:
            try:
                status = request({'cmd': 'status'})
                if (status.get('mode') == 'auto' and status.get('requested_mode') == 'auto'
                        and not status.get('last_error') and not status.get('emergency')):
                    check_readings(status, 5, max_stage=True)
                    registers = read_registers()
                    if registers == expected:
                        return new_pid, status, registers
            except (OSError, RuntimeError, ValueError):
                pass
        time.sleep(0.25)
    raise RuntimeError('Service did not recover with a fresh PID, firmware Auto and baseline bytes')


def check_readings(status: dict, elapsed: float, *, max_stage: bool = False) -> None:
    cpu = status.get('temps', {}).get('cpu')
    if type(cpu) not in (int, float) or not math.isfinite(cpu) or not -50 <= cpu < MAX_STAGE_ABORT_C:
        raise RuntimeError(f'Validation requires a readable CPU below {MAX_STAGE_ABORT_C} C, got {cpu}')
    guardian_cpu = status.get('safety', {}).get('temps', {}).get('cpu')
    if type(guardian_cpu) in (int, float) and math.isfinite(guardian_cpu):
        cpu = max(cpu, guardian_cpu)
        if cpu >= MAX_STAGE_ABORT_C:
            raise RuntimeError(f'Guardian CPU reached the validation abort limit: {cpu} C')
    if status.get('last_error'):
        raise RuntimeError(f'Controller reported a fault: {status}')
    for fan in ('fan1', 'fan2'):
        rpm = status.get('fans', {}).get(fan, {}).get('rpm')
        if type(rpm) is not int or rpm < 0 or (elapsed >= 5 and rpm == 0):
            raise RuntimeError(f'{fan} tachometer is unavailable or stopped')
    if not max_stage and (cpu >= LOW_SPEED_LIMIT_C or status.get('emergency')):
        raise ThermalHold(f'Low-speed/Auto observation needs CPU below {LOW_SPEED_LIMIT_C} C '
                          f'without an emergency override; got {cpu} C', status)


def sample(stage: str, duration: int, target: int | None, baseline: dict) -> None:
    started = time.monotonic()
    while (elapsed := time.monotonic() - started) < duration:
        status = request({'cmd': 'status'})
        check_readings(status, elapsed, max_stage=target == 100)
        registers = read_registers()
        if elapsed >= 5:
            if target is None and registers != baseline:
                raise RuntimeError('Firmware Auto has not restored the saved EC bytes')
            if target is not None:
                if any(registers[key] != target for key in ('0x37', '0x3a')):
                    raise RuntimeError('Native NBFC target bytes differ from the requested speed')
                if registers['0x22'] != 0x0c or registers['0x21'] != 0x30:
                    raise RuntimeError('The EC did not enter the expected manual control modes')
                if any(status['fans'][fan]['percent'] != target for fan in ('fan1', 'fan2')):
                    raise RuntimeError('The service lost the requested manual target')
                if target == 100 and status.get('requested_mode') != 'max':
                    raise RuntimeError('Max observation requires a persistent Max request')
        record('sample', stage=stage, elapsed_s=round(elapsed, 2), status=status, registers=registers)
        time.sleep(1)


def validate(seconds: int, crash_only: bool = False) -> bool:
    active = subprocess.run(['systemctl', 'is-active', '--quiet', 'nitrofan.service'])
    if active.returncode not in (3, 4):
        raise RuntimeError('Validation requires the service to be stopped')
    configuration = tomllib.loads(CONFIG.read_text())
    policy_path = Path(configuration['nbfc'].get(
        'policy_path', '/opt/nitrofan/tools/ec_guard/config/ec_guard_control_policy.json'))
    policy = json.loads(policy_path.read_text())
    expected = {f'0x{int(offset, 16):02x}': value
                for offset, value in policy['expected_baselines'].items()}
    # The timer survives this terminal or validator dying. It is deliberately
    # outside the fan daemon's cgroup so the crash test cannot remove it.
    timer_seconds = (2 * seconds + 90) if crash_only else (5 * seconds + 90)
    subprocess.run(['systemd-run', '--quiet', '--collect',
                    '--unit=nitrofan-validation-stop', f'--on-active={timer_seconds}s',
                    '/usr/bin/systemctl', 'stop', 'nitrofan.service'], check=True, timeout=15)
    started = False
    original_exception = None
    completed = []
    pending = []
    try:
        started = True
        subprocess.run(['systemctl', 'start', 'nitrofan.service'], check=True, timeout=40)
        status = wait_auto()
        check_readings(status, 0, max_stage=True)
        baseline = read_registers()
        if baseline != expected:
            raise RuntimeError('Initial EC bytes do not match the configured baseline')
        record('begin', baseline=baseline, seconds_per_stage=seconds, crash_only=crash_only)
        if not crash_only:
            floor = configuration.get('min_percent', 25)
            targets = sorted({floor, *(target for target in (30, 60) if target >= floor)} - {100})
            for index, target in enumerate(targets):
                stage = f'manual-{target}'
                print(f'Checking {stage} for {seconds} seconds', file=sys.stderr, flush=True)
                try:
                    # Check before lowering either fan, as well as during each sample.
                    check_readings(request({'cmd': 'status'}), 5)
                    request({'cmd': 'set_mode', 'mode': 'fixed', 'percent': target})
                    sample(stage, seconds, target, baseline)
                    completed.append(stage)
                except ThermalHold as hold:
                    request({'cmd': 'set_mode', 'mode': 'max'})
                    pending.extend(f'manual-{value}' for value in targets[index:])
                    record('lower_speeds_pending', reason=str(hold), status=hold.status,
                           pending=list(pending))
                    break
        print(f'Checking maximum speed for {seconds} seconds before crash test', file=sys.stderr, flush=True)
        request({'cmd': 'set_mode', 'mode': 'max'})
        sample('max', seconds, 100, baseline)
        completed.append('max')
        pre_crash_pid = get_main_pid('nitrofan.service')
        if pre_crash_pid <= 0:
            raise RuntimeError('Cannot determine nitrofan.service MainPID before crash test')
        print(f'Checking daemon crash recovery at maximum speed (PID {pre_crash_pid})', file=sys.stderr, flush=True)
        subprocess.run(['systemctl', 'kill', '--kill-whom=main', '--signal=SIGKILL',
                        'nitrofan.service'], check=True, timeout=10)
        new_pid, recovered_status, recovered_registers = wait_crash_recovery(pre_crash_pid, expected)
        record('crash_recovered', pid=new_pid, old_pid=pre_crash_pid,
               status=recovered_status, registers=recovered_registers)
        completed.append('crash_recovery')
        request({'cmd': 'set_mode', 'mode': 'auto'})
        try:
            sample('firmware-auto', seconds, None, baseline)
            completed.append('extended_auto_observation')
        except ThermalHold as hold:
            pending.append('extended_auto_observation')
            record('auto_observation_pending', reason=str(hold), status=hold.status)
    except BaseException as error:
        original_exception = error
        raise
    finally:
        if started:
            previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
            previous_sigterm = signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                # systemd's ExecStopPost runs the independent recovery command even
                # if the API or its supervising process has failed.
                stop = subprocess.run(['systemctl', 'stop', 'nitrofan.service'], timeout=40)
                final = read_registers()
                write_support_value = WRITE_SUPPORT.read_text().strip()
                if stop.returncode or final != expected or write_support_value != 'N':
                    raise RuntimeError('Service shutdown did not verify baseline bytes and write_support=N')
                record('cleanup', restored=True, registers=final, write_support='N')
                subprocess.run(['systemctl', 'stop', 'nitrofan-validation-stop.timer'],
                               check=True, timeout=10)
            except BaseException:
                if original_exception:
                    print('Cleanup verification also failed; inspect the systemd journal before further control.',
                          file=sys.stderr)
                raise
            finally:
                signal.signal(signal.SIGINT, previous_sigint)
                signal.signal(signal.SIGTERM, previous_sigterm)
    completed.append('shutdown_firmware_handoff')
    record('partial' if pending else 'passed', completed=completed, pending=pending,
           scope='max_and_crash' if crash_only else 'speed_and_crash',
           note='Pending observations need cooler conditions; suspend/resume is separate')
    return not pending


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds-per-stage', type=int, default=20, choices=range(10, 61),
                        metavar='10..60')
    parser.add_argument('--crash-only', action='store_true',
                        help='Verify daemon crash recovery and auto restoration only, skipping lower speeds')
    args = parser.parse_args()
    if os.geteuid() != 0 or sys.platform != 'linux':
        parser.error('Run with sudo on the Linux laptop after installing the backend')

    def stop_on_signal(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_on_signal)
    signal.signal(signal.SIGINT, stop_on_signal)
    try:
        passed = validate(args.seconds_per_stage, crash_only=args.crash_only)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        record('failed', error=str(error) or 'Interrupted')
        print(f'Backend validation failed: {error}', file=sys.stderr)
        return 1
    return 0 if passed else 2


if __name__ == '__main__':
    raise SystemExit(main())
