#!/usr/bin/env python3
"""Hold guarded Max until CPU/Acer stay below 70 C, then restore firmware Auto."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys

try:
    from tools.validate_suspend_resume import SystemRunner, check_released, expected_baseline
except ModuleNotFoundError:
    from validate_suspend_resume import SystemRunner, check_released, expected_baseline

UNIT = 'nitrofan.service'
TIMER = 'nitrofan-cooldown-stop'
TARGET_C = 70.0
HOLD_SECONDS = 30
MAX_SECONDS = 540


def cool_down(runner: SystemRunner) -> bool:
    baseline = expected_baseline(runner)
    check_released(runner, baseline)
    runner.run(['systemd-run', '--quiet', '--collect', f'--unit={TIMER}',
                '--on-active=600s', '/usr/bin/systemctl', 'stop', UNIT], check=True)
    cooled = False
    try:
        runner.run(['systemctl', 'start', UNIT], check=True, timeout=40)
        deadline = runner.monotonic() + 30
        while True:
            try:
                runner.request({'cmd': 'set_mode', 'mode': 'max'})
                break
            except OSError:
                if runner.monotonic() >= deadline:
                    raise
                runner.sleep(0.25)
        started = runner.monotonic()
        cool_since = None
        while runner.monotonic() - started < MAX_SECONDS:
            status = runner.request({'cmd': 'status'})
            if status.get('last_error'):
                raise RuntimeError(f'Fan controller fault: {status["last_error"]}')
            if status.get('mode') not in ('max', 'emergency'):
                raise RuntimeError('Service no longer holds guarded Max')
            readings = [status.get('temps', {})]
            guardian = status.get('safety', {}).get('temps')
            if guardian:
                readings.append(guardian)
            temperatures = []
            for reading in readings:
                for sensor in ('cpu', 'acer_temp1'):
                    value = reading.get(sensor)
                    if type(value) not in (int, float) or not math.isfinite(value) or not -50 <= value <= 150:
                        raise RuntimeError(f'Cooling sensor {sensor} is unavailable or invalid')
                    temperatures.append(value)
            fans = status.get('fans', {})
            if any(type(fans.get(fan, {}).get('rpm')) is not int or fans[fan]['rpm'] <= 0
                   for fan in ('fan1', 'fan2')):
                raise RuntimeError('A fan tachometer is unavailable or stopped')
            print(f'Max. CPU {readings[0]["cpu"]} C, Acer {readings[0]["acer_temp1"]} C; '
                  f'fans {fans["fan1"]["rpm"]}/{fans["fan2"]["rpm"]} RPM', flush=True)
            now = runner.monotonic()
            cool_since = (now if cool_since is None else cool_since) if max(temperatures) < TARGET_C else None
            if cool_since is not None and now - cool_since >= HOLD_SECONDS:
                cooled = True
                break
            runner.sleep(2)
    finally:
        previous = {signum: signal.signal(signum, signal.SIG_IGN)
                    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            runner.run(['systemctl', 'stop', UNIT], check=True, timeout=40)
            check_released(runner, baseline)
            for suffix in ('timer', 'service'):
                result = runner.run(['systemctl', 'stop', f'{TIMER}.{suffix}'], check=False,
                                    capture_output=True, text=True)
                if result.returncode not in (0, 5):
                    raise RuntimeError(f'Cannot disarm cooldown stop timer: {result.stderr}')
            print('Acer Auto restored; EC writes disabled.', flush=True)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    print('Cooling target reached.' if cooled else 'Cooling time limit reached.', flush=True)
    return cooled


def main() -> int:
    if os.geteuid() != 0 or sys.platform != 'linux':
        print('Run with sudo on basement-server.', file=sys.stderr)
        return 1

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        return 0 if cool_down(SystemRunner()) else 2
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f'Cooldown stopped: {error or "Interrupted"}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
