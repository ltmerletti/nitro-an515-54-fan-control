#!/usr/bin/env python3
"""Install the backend and systemd units without starting fan control."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

SOURCE = Path(__file__).resolve().parent.parent
PREFIX = Path('/opt/nitrofan')
CONFIG = Path('/etc/nitrofan/config.toml')
PINNED_COMMIT = '9da010d6a2e8351bdf6900d6c3643f1e742af936'
SERVICE_UNITS = ('nitrofan.service', 'nitrofan-sleep.service', 'nitrofan-suspend-check.service')


def run(*command: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, **kwargs)


def check_destination(path: Path) -> None:
    """Root installs must not follow a user-controlled link or directory."""
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise RuntimeError(f'Refusing symlink in install destination: {parent}')
        if parent.exists():
            metadata = parent.stat()
            if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                raise RuntimeError(f'Install destination must be owned by root and not writable by others: {parent}')


def copy_tree(source: Path, destination: Path) -> None:
    check_destination(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name == '__pycache__' or item.suffix in ('.pyc', '.o', '.so', '.dylib'):
            continue
        if item.name in ('guarded_nbfc_manifest.json', 'test_ec_guard', 'bin'):
            continue
        if item.is_symlink():
            raise RuntimeError(f'Refusing symlink in installation source: {item}')
        target = destination / item.name
        if item.is_dir():
            copy_tree(item, target)
        elif item.is_file():
            copy_file(item, target)


def copy_file(source: Path, destination: Path, mode: int = 0o644) -> None:
    check_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)


def install_dependencies() -> None:
    environment = dict(os.environ, DEBIAN_FRONTEND='noninteractive',
                       NEEDRESTART_MODE='l', APT_LISTCHANGES_FRONTEND='none')
    options = {'stdin': subprocess.DEVNULL, 'env': environment}
    print('Installing system build dependencies...', flush=True)
    run('apt-get', '-o', 'Dpkg::Use-Pty=0', 'update', **options)
    run('apt-get', '-o', 'Dpkg::Use-Pty=0', 'install', '-y',
        'gcc', 'git', 'patch', 'liblua5.4-dev', 'python3', 'procps', **options)


def validate_installation() -> None:
    result = subprocess.run([sys.executable, str(PREFIX / 'tools/validate_backend.py')], check=False)
    if result.returncode == 2:
        print('Backend installed. Validation is partial; review completed/pending stages. '
              'Boot startup remains disabled.', flush=True)
    elif result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args)


def install(args: argparse.Namespace) -> None:
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise RuntimeError('Run the installer with sudo on the Linux laptop')
    if sys.version_info < (3, 12):
        raise RuntimeError('Python 3.12 or newer is required')
    sys.path.insert(0, str(SOURCE))
    from nitrofan.models import detect_model
    model = detect_model()
    if model.model_id != 'AN515-54' or not model.capabilities.guarded_max_trial:
        raise RuntimeError('The backend installer currently requires Acer Nitro AN515-54 BIOS V1.33')
    for destination in (PREFIX, CONFIG, *(Path('/etc/systemd/system') / unit for unit in SERVICE_UNITS),
                        Path('/etc/modprobe.d/nitrofan-acer.conf')):
        check_destination(destination)
    for unit in SERVICE_UNITS:
        state = run('systemctl', 'show', '--property=ActiveState', '--value', unit,
                    capture_output=True, text=True).stdout.strip()
        if state not in ('inactive', 'failed', ''):
            raise RuntimeError(f'Stop {unit} before upgrading its files')
    if (PREFIX / 'runtime/session.marker').exists():
        raise RuntimeError('An active recovery record exists; recover the previous service before upgrading')
    if Path('/var/lib/nitrofan-validation/suspend.json').exists():
        raise RuntimeError('Finish or cancel the pending suspend validation before upgrading')
    if args.install_dependencies:
        install_dependencies()
    for command in ('cc', 'git', 'patch', 'systemctl', 'modprobe', 'pgrep'):
        if shutil.which(command) is None:
            raise RuntimeError(f'Missing {command}; install the build dependencies first')
    if not Path('/usr/include/lua5.4/lua.h').is_file():
        raise RuntimeError('Missing system Lua development headers; use --install-dependencies')
    source = args.source_dir.resolve(strict=True)
    # Clone committed upstream objects into root-owned storage. No user hooks or
    # working-tree edits are copied into the build.
    with tempfile.TemporaryDirectory(prefix='nitrofan-install-') as temporary:
        upstream = Path(temporary) / 'upstream'
        run('git', 'clone', '--no-hardlinks', '--no-checkout', '--', str(source), str(upstream))
        run('git', '-C', str(upstream), 'checkout', '--detach', PINNED_COMMIT)
        copy_tree(SOURCE / 'nitrofan', PREFIX / 'nitrofan')
        copy_tree(SOURCE / 'profiles', PREFIX / 'profiles')
        copy_tree(SOURCE / 'tools/ec_guard', PREFIX / 'tools/ec_guard')
        for filename in ('nbfc_guarded_trial.py', 'service_hardware.py',
                         'validate_backend.py', 'validate_suspend_resume.py'):
            copy_file(SOURCE / 'tools' / filename, PREFIX / 'tools' / filename)
        copy_file(SOURCE / 'examples/nbfc.toml', PREFIX / 'examples/nbfc.toml')
        if not CONFIG.exists():
            copy_file(PREFIX / 'examples/nbfc.toml', CONFIG, 0o640)
        configuration = tomllib.loads(CONFIG.read_text())
        if configuration.get('backend') != 'nbfc' or configuration.get('startup_mode') != 'auto':
            raise RuntimeError(f'Preserved {CONFIG} must select backend="nbfc" and startup_mode="auto"')
        nbfc = configuration['nbfc']
        if any(nbfc.get(field) for field in ('synthetic_root', 'ec_path', 'sysfs_root', 'service_bin')):
            raise RuntimeError('Installed service configuration must use real system hardware and the attested binary')
        if nbfc['runtime_dir'] != str(PREFIX / 'runtime'):
            raise RuntimeError('Installed native paths require runtime_dir=/opt/nitrofan/runtime')
        manifest = Path(nbfc['manifest_path'])
        if manifest != PREFIX / 'build/manifest.json':
            raise RuntimeError('Installed manifest must be /opt/nitrofan/build/manifest.json')
        check_destination(manifest)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        policy = Path(nbfc.get('policy_path', str(PREFIX / 'tools/ec_guard/config/ec_guard_control_policy.json')))
        profile = Path(nbfc.get('profile_path', str(PREFIX / 'tools/ec_guard/profiles/Acer_Nitro_AN515-54_guarded_control.json')))
        for artifact in (policy, profile):
            if not artifact.resolve().is_relative_to(PREFIX) or not artifact.is_file():
                raise RuntimeError(f'Installed policy/profile is missing or outside {PREFIX}: {artifact}')
        run(sys.executable, str(PREFIX / 'tools/ec_guard/build_guarded_nbfc.py'),
            '--source-dir', str(upstream), '--private-root', str(PREFIX / 'runtime'),
            '--output-dir', str(PREFIX / 'build'), '--manifest-path', str(manifest),
            '--policy-path', str(policy), '--profile-path', str(profile),
            '--lua-include', '/usr/include/lua5.4', '--lua-lib-dir', '/usr/lib',
            '--multiarch-include', '/usr/include')
    group = subprocess.run(['getent', 'group', 'nitrofan'], capture_output=True)
    if group.returncode == 2:
        run('groupadd', '--system', 'nitrofan')
    elif group.returncode:
        raise RuntimeError('Cannot check the nitrofan group')
    if args.client_user:
        if args.client_user == 'root' or args.client_user.startswith('-'):
            raise RuntimeError('Choose a non-root client username')
        run('id', '--', args.client_user)
        run('usermod', '-a', '-G', 'nitrofan', '--', args.client_user)
    for filename in SERVICE_UNITS:
        copy_file(SOURCE / 'packaging' / filename, Path('/etc/systemd/system') / filename)
    module_options = Path('/etc/modprobe.d/nitrofan-acer.conf')
    if not module_options.exists():
        module_options.write_text('options acer_wmi predator_v4=1\n')
        module_options.chmod(0o644)
    (PREFIX / 'runtime').chmod(0o700)
    run('systemctl', 'daemon-reload')
    run('systemd-analyze', 'verify', *(str(Path('/etc/systemd/system') / unit) for unit in SERVICE_UNITS))
    print('Backend installed. Services remain stopped and are not enabled at boot.')
    if args.validate:
        validate_installation()
    else:
        print('Run sudo /usr/bin/python3 /opt/nitrofan/tools/validate_backend.py before enabling them.')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, required=True,
                        help='Existing pinned nbfc-linux Git checkout')
    parser.add_argument('--install-dependencies', action='store_true')
    parser.add_argument('--validate', action='store_true',
                        help='Run the bounded hardware check after installation; leaves the service stopped')
    parser.add_argument('--client-user', default=os.environ.get('SUDO_USER'))
    args = parser.parse_args()
    try:
        install(args)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Installation failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
