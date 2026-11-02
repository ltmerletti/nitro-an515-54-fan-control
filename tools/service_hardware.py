#!/usr/bin/env python3
"""Systemd lifecycle hooks for the guarded NBFC backend."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nitrofan.models import detect_model
from nitrofan.backends.hwmon import HwmonBackend
from nitrofan.sensors import SystemSensorProvider

WRITE_SUPPORT = Path('/sys/module/ec_sys/parameters/write_support')
SLEEP_MARKER = Path('/run/nitrofan-resume')
CONFIG = '/etc/nitrofan/config.toml'


def run(*command: str, timeout: int = 20) -> None:
    subprocess.run(command, check=True, timeout=timeout, cwd=ROOT)


def verify_model() -> None:
    match = detect_model()
    if match.model_id != 'AN515-54' or not match.capabilities.guarded_max_trial:
        raise RuntimeError('The service currently requires Acer Nitro AN515-54 BIOS V1.33')


def enable_writes() -> None:
    verify_model()
    # A stock NBFC service would bypass the NitroFan write policy.
    result = subprocess.run(['pgrep', '-x', 'nbfc_service'], capture_output=True, check=False)
    if result.returncode != 1:
        raise RuntimeError('Cannot start while another NBFC instance is running')
    telemetry = HwmonBackend()
    try:
        telemetry.open()
        if any((telemetry.read_rpm(fan) or 0) <= 0 for fan in ('fan1', 'fan2')):
            raise RuntimeError('Both Acer tachometers must be readable and spinning before startup')
        if SystemSensorProvider()().get('cpu') is None:
            raise RuntimeError('CPU temperature is unavailable')
    finally:
        telemetry.close()
    if not os.path.ismount('/sys/kernel/debug'):
        raise RuntimeError('The required sys-kernel-debug.mount is not mounted')
    if WRITE_SUPPORT.exists() and WRITE_SUPPORT.read_text().strip() not in ('Y', '1'):
        run('modprobe', '-r', 'ec_sys')
    run('modprobe', 'ec_sys', 'write_support=1')
    if WRITE_SUPPORT.read_text().strip() not in ('Y', '1'):
        raise RuntimeError('ec_sys write support could not be enabled')


def release_control() -> None:
    # Recovery must succeed before write support is disabled. Keep the session
    # record available for another recovery attempt if restoration fails.
    run(sys.executable, '-m', 'nitrofan.nbfc_guardian', '--recover', '--config', CONFIG)
    result = subprocess.run(['pgrep', '-x', 'nbfc_service'], capture_output=True, check=False)
    if result.returncode != 1:
        raise RuntimeError('NBFC is still running; refusing to unload its EC driver')
    if WRITE_SUPPORT.exists():
        run('modprobe', '-r', 'ec_sys')
        run('modprobe', 'ec_sys', 'write_support=0')
        if WRITE_SUPPORT.read_text().strip() not in ('N', '0'):
            raise RuntimeError('EC write protection could not be restored')


def prepare_sleep() -> None:
    result = subprocess.run(['systemctl', 'is-active', '--quiet', 'nitrofan.service'], check=False)
    if result.returncode == 0:
        SLEEP_MARKER.write_text('restart\n')
        run('systemctl', 'stop', 'nitrofan.service', timeout=35)
        # A failed ExecStopPost must prevent this hook from reporting success.
        release_control()
    elif result.returncode not in (3, 4):
        raise RuntimeError('Cannot determine whether NitroFan is running')


def resume_after_sleep() -> None:
    if SLEEP_MARKER.exists():
        run('systemctl', 'start', 'nitrofan.service', timeout=35)
        SLEEP_MARKER.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'sleep', 'wake'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Systemd lifecycle hooks require root')
    try:
        {'start': enable_writes, 'stop': release_control,
         'sleep': prepare_sleep, 'wake': resume_after_sleep}[args.action]()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'NitroFan lifecycle failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
