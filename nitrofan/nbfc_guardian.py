"""
nbfc_guardian.py - Independent guardian process and recovery for NitroFan NBFC backend.

Supervises the upstream NBFC child process, manages durable session markers,
enforces safety bounds (emergency cooling, CPU sensor loss, fan stalls),
and provides external recovery (ExecStopPost) to restore firmware Auto if the
guardian or daemon crashes or is terminated with SIGKILL.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time
from typing import Any

from nitrofan.config import Config, load_config
from nitrofan.sensors import SystemSensorProvider
from tools.nbfc_guarded_trial import ECGuardBinding, GuardedTrialSupervisor, strict_json

logger = logging.getLogger("nitrofan.nbfc_guardian")

DEFAULT_MANIFEST_PATH = Path("/opt/nitrofan/build/manifest.json")
DEFAULT_RUNTIME_DIR = Path("/opt/nitrofan/runtime")
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY_PATH = REPO_ROOT / "tools" / "ec_guard" / "config" / "ec_guard_control_policy.json"
DEFAULT_PROFILE_PATH = REPO_ROOT / "tools" / "ec_guard" / "profiles" / "Acer_Nitro_AN515-54_guarded_control.json"
PARENT_TIMEOUT_S = 30.0
FRAME_LIMIT = 4096

EXPECTED_DMI = {
    "sys_vendor": "Acer",
    "product_name": "Nitro AN515-54",
    "board_name": "Octavia_CFS",
    "bios_version": "V1.33",
}


def get_boot_id(synthetic_root: Path | None = None) -> str:
    """Read machine boot ID from /proc/sys/kernel/random/boot_id or synthetic sandbox."""
    if synthetic_root is not None:
        boot_path = synthetic_root / "proc/sys/kernel/random/boot_id"
        if boot_path.exists():
            return boot_path.read_text().strip()
        # Fallback to test environment variable or fixed synthetic ID
        return os.environ.get("NITROFAN_TEST_BOOT_ID", "synthetic-boot-an515-54")
    value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not value:
        raise RuntimeError("Machine boot ID is unavailable")
    return value


def process_identity(pid: int) -> str | None:
    """Linux start time prevents a recycled PID from becoming a kill target."""
    process_path = Path(f"/proc/{pid}")
    try:
        fields = (process_path / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] != "Z":
            return fields[19]
        # A Linux thread-group leader can be a zombie while other threads still
        # run. Keep ownership until those threads stop, before restoring the EC.
        for task in (process_path / "task").iterdir():
            try:
                state = (task / "stat").read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                continue
            if state not in ("Z", "X"):
                return fields[19]
        return None
    except FileNotFoundError:
        return None


def stop_process_group(pid: int, identity: str) -> None:
    current = process_identity(pid)
    if current is None:
        return
    if current != identity or os.getpgid(pid) != pid:
        raise RuntimeError("Refusing to signal a changed NBFC process identity")
    for signum, duration in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
        os.killpg(pid, signum)
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if process_identity(pid) is None:
                return
            time.sleep(0.05)
    raise RuntimeError("NBFC writer did not stop; restoration is blocked")


def require_private_root(path: Path) -> None:
    for parent in (path, *path.parents):
        metadata = parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise PermissionError(f"Real backend path must be root-owned and not writable by others: {parent}")


def compute_file_sha256(path: Path) -> str:
    """Compute SHA-256 digest of a file."""
    hasher = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_dmi(dmi_root: Path) -> None:
    """Verify hardware identity against expected AN515-54 V1.33 attributes."""
    for key, expected in EXPECTED_DMI.items():
        attr_file = dmi_root / key
        if not attr_file.is_file():
            raise RuntimeError(f"Missing DMI attribute: {key}")
        value = attr_file.read_text().strip()
        if value != expected:
            raise RuntimeError(
                f"Hardware mismatch for {key}: '{value}', expected '{expected}'"
            )


def write_durable_marker(marker_path: Path, marker_data: dict[str, Any]) -> None:
    """
    Write and fsync session marker before any active writes are performed.
    Guarantees persistence across process crashes.
    """
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = marker_path.with_name(f"{marker_path.name}.tmp.{os.getpid()}")
    content = json.dumps(marker_data, indent=2).encode("utf-8")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp_path, marker_path)

    dir_fd = os.open(marker_path.parent, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class NBFCGuardian:
    """
    Guardian supervisor owning the NBFC child process, C guard snapshot,
    and safety sensors.
    """

    def __init__(
        self,
        config: Config | None = None,
        runtime_dir: str | Path | None = None,
        manifest_path: str | Path | None = None,
        policy_path: str | Path | None = None,
        profile_path: str | Path | None = None,
        service_bin: str | Path | None = None,
        ec_path: str | Path | None = None,
        sysfs_root: str | Path | None = None,
        synthetic_root: str | Path | None = None,
        parent_in_fd: int = 0,
        parent_out_fd: int = 1,
    ) -> None:
        self.config = config
        nbfc_cfg = config.nbfc if config else None

        self.synthetic_root = Path(synthetic_root).resolve() if synthetic_root else (
            Path(nbfc_cfg.synthetic_root).resolve() if nbfc_cfg and nbfc_cfg.synthetic_root else None
        )
        self.is_synthetic = self.synthetic_root is not None

        raw_runtime = runtime_dir or (nbfc_cfg.runtime_dir if nbfc_cfg else None) or (
            self.synthetic_root / "runtime" if self.is_synthetic else DEFAULT_RUNTIME_DIR
        )
        self.runtime_dir = Path(raw_runtime).resolve()

        self.manifest_path = Path(
            manifest_path or (nbfc_cfg.manifest_path if nbfc_cfg else None) or DEFAULT_MANIFEST_PATH
        ).resolve()

        self.policy_path = Path(
            policy_path or (nbfc_cfg.policy_path if nbfc_cfg else None) or DEFAULT_POLICY_PATH
        ).resolve()

        self.profile_path = Path(
            profile_path or (nbfc_cfg.profile_path if nbfc_cfg else None) or DEFAULT_PROFILE_PATH
        ).resolve()

        self._service_explicit = bool(service_bin or (nbfc_cfg.service_bin if nbfc_cfg else None))
        self.service_bin = Path(service_bin or (nbfc_cfg.service_bin if nbfc_cfg else None) or (
            self.runtime_dir / "nbfc_service"
        )).resolve()

        self.sysfs_root = Path(sysfs_root or (nbfc_cfg.sysfs_root if nbfc_cfg else None) or (
            self.synthetic_root / "sys" if self.is_synthetic else Path("/sys")
        )).resolve()

        self.ec_path = Path(ec_path or (nbfc_cfg.ec_path if nbfc_cfg else None) or (
            self.synthetic_root / "fake_ec.bin" if self.is_synthetic else Path("/sys/kernel/debug/ec/ec0/io")
        )).resolve()

        self.dmi_root = Path(
            self.synthetic_root / "sys/class/dmi/id" if self.is_synthetic else Path("/sys/class/dmi/id")
        ).resolve()

        self.lock_file_path = (self.runtime_dir / "nbfc_guardian.lock" if self.is_synthetic else
                               Path("/run/lock/nitrofan_guarded_trial.lock"))
        self.marker_path = self.runtime_dir / "session.marker"
        self.audit_log = self.runtime_dir / "ec_guard_audit.log"
        self.state_file = self.runtime_dir / "var/lib/nbfc/state.json"

        self.parent_in_fd = parent_in_fd
        self.parent_out_fd = parent_out_fd

        self.guard: ECGuardBinding | None = None
        self.ec_fd: int | None = None
        self.lock_fd: int | None = None
        self.child_process: subprocess.Popen | None = None

        self.current_percent: int | None = None
        self.emergency: bool = False
        self.cpu_missing_count: int = 0
        self.stop_requested: bool = False
        self.last_parent_message_time: float = time.monotonic()
        self.cleaned_up: bool = False
        self._baseline_captured = False
        self._owns_marker = False
        self._child_identity: str | None = None
        self._emergency_sources: set[str] = set()
        self.last_temps: dict[str, float | None] = {}
        self.emergency_trigger_temps: dict[str, float] = {}
        self.fault: str | None = None
        self._requested_percent: int | None = None
        self.library_path: Path | None = None
        self._cpu_path: Path | None = None
        self._last_sensor_check = -1e9

    def attest_build(self) -> None:
        """Reuse trial provenance checks for the binary, guard and profile."""
        verifier = GuardedTrialSupervisor(
            manifest_path=self.manifest_path, policy_path=self.policy_path,
            profile_path=self.profile_path, synthetic_root=self.synthetic_root,
            service_bin=self.service_bin if self._service_explicit else None,
        )
        verifier._attest_build()
        if verifier.runtime_root != self.runtime_dir:
            raise ValueError("Configured runtime differs from NBFC's compiled private paths")
        self.service_bin = verifier.service_bin
        self.library_path = verifier.library_path
        for path in (self.service_bin, self.library_path):
            if self.is_synthetic:
                if not path.resolve().is_relative_to(self.synthetic_root):
                    raise PermissionError("Synthetic binary/library must stay in the sandbox")
            else:
                require_private_root(path)
        if not os.access(self.service_bin, os.X_OK):
            raise RuntimeError("The attested NBFC binary is not executable")
        policy = strict_json(self.policy_path)
        allowed = {rule['offset']: set(rule['allowed_values']) for rule in policy['allowed_writes']}
        expected = {0x10: {0, 1}, 0x21: {16, 48}, 0x22: {4, 12},
                    0x37: set(range(25, 101)), 0x38: {0},
                    0x3a: set(range(25, 101)), 0x3b: {0}}
        if allowed != expected:
            raise ValueError("Control policy differs from the reviewed -54 register envelope")
        profile = strict_json(self.profile_path)
        fans = profile.get('FanConfigurations', [])
        if (profile.get('ReadWriteWords') is not True or len(fans) != 2 or
                profile.get('CriticalTemperature') != 90):
            raise ValueError("Unexpected control profile layout or thermal limit")
        for fan, read, write in zip(fans, (0x13, 0x15), (0x37, 0x3a)):
            if (fan.get('ReadRegister') != read or fan.get('WriteRegister') != write or
                    fan.get('MinSpeedValue') != 0 or fan.get('MaxSpeedValue') != 100 or
                    fan.get('FanSpeedResetValue') != 100 or
                    not fan.get('TemperatureThresholds') or
                    any(point.get('FanSpeed') != 100 for point in fan['TemperatureThresholds'])):
                raise ValueError("Profile speed scaling, reset or fallback is invalid")
        for register in profile.get('RegisterWriteConfigurations', []):
            values = allowed.get(register.get('Register'), set())
            if (register.get('WriteMode') != 'Set' or register.get('ResetWriteMode') != 'Set' or
                    register.get('Value') not in values or register.get('ResetValue') not in values):
                raise ValueError("Profile initialization/reset exceeds the control policy")
        if 'Lua' in json.dumps(profile) or 'Acpi' in json.dumps(profile):
            raise ValueError("Executable profile hooks are not supported")

    def acquire_lock(self) -> None:
        """Acquire single-instance guardian lock."""
        if self.lock_fd is not None:
            return
        fd = os.open(self.lock_file_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            stat_res = os.fstat(fd)
            if (not stat.S_ISREG(stat_res.st_mode) or stat_res.st_uid != os.geteuid() or
                    stat_res.st_mode & 0o077):
                raise RuntimeError("Guardian lock must be a private file owned by the controller user")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as e:
            os.close(fd)
            raise RuntimeError(f"Could not acquire guardian lock: {e}") from e
        self.lock_fd = fd

    def release_lock(self) -> None:
        """Release single-instance guardian lock."""
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                os.close(self.lock_fd)
            except OSError:
                pass
            self.lock_fd = None

    def preflight(self, recovering: bool = False) -> None:
        """Validate paths, sandbox boundaries, hardware DMI, and policy files."""
        if self.is_synthetic:
            # Enforce synthetic sandbox containment
            for path in (self.ec_path, self.sysfs_root, self.runtime_dir):
                if not path.is_relative_to(self.synthetic_root):
                    raise PermissionError(f"Synthetic path escapes sandbox: {path}")
            if not self.ec_path.is_file() or self.ec_path.stat().st_size != 256:
                raise ValueError("Synthetic EC must be a regular 256-byte file")
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
        else:
            # Real hardware mode: reject injected arbitrary paths
            if self.ec_path != Path("/sys/kernel/debug/ec/ec0/io"):
                raise ValueError(f"Real mode requires verified EC path /sys/kernel/debug/ec/ec0/io, got {self.ec_path}")
            if self.sysfs_root != Path("/sys"):
                raise ValueError("Real mode requires system /sys")
            if os.geteuid() != 0:
                raise PermissionError("Real hardware mode requires root")
            verify_dmi(self.dmi_root)
            for path in (REPO_ROOT, self.runtime_dir, self.manifest_path,
                         self.policy_path, self.profile_path):
                require_private_root(path)
            if Path('/sys/module/ec_sys/parameters/write_support').read_text().strip() != 'Y':
                raise RuntimeError("Guarded control requires ec_sys write_support=Y")

        if not self.manifest_path.is_file():
            raise FileNotFoundError(f"Manifest file not found: {self.manifest_path}")

        if not self.policy_path.is_file():
            raise FileNotFoundError(f"Policy file not found: {self.policy_path}")

        self.attest_build()
        self.acquire_lock()
        if not recovering and (self.marker_path.exists() or self.marker_path.is_symlink()):
            raise RuntimeError("An existing session requires external recovery before startup")

    def init_ec_guard(self) -> None:
        """Initialize C guard and capture baseline snapshot."""
        self.guard = ECGuardBinding(self.library_path)
        init_res = self.guard.init(str(self.policy_path), str(self.audit_log))
        if init_res != 0:
            raise RuntimeError(f"Failed to initialize ec_guard policy: {init_res}")

        self.ec_fd = os.open(self.ec_path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        snap_res = self.guard.capture_snapshot(self.ec_fd)
        if snap_res != 0:
            raise RuntimeError(
                f"EC baseline mismatch or verification failure (code={snap_res}); zero writes permitted"
            )
        self._baseline_captured = True

    def write_marker(self) -> None:
        """Create and fsync session marker before active writes."""
        boot_id = get_boot_id(self.synthetic_root)
        manifest_hash = compute_file_sha256(self.manifest_path)
        policy_hash = compute_file_sha256(self.policy_path)
        profile_hash = compute_file_sha256(self.profile_path)

        marker_data = {
            "version": 1,
            "boot_id": boot_id,
            "manifest_sha256": manifest_hash,
            "policy_sha256": policy_hash,
            "profile_sha256": profile_hash,
            "ec_path": str(self.ec_path),
            "guardian_pid": os.getpid(),
            "nbfc_pid": self.child_process.pid if self.child_process else None,
            "nbfc_identity": self._child_identity,
            "baseline_captured": self._baseline_captured,
            "timestamp": time.time(),
            "active": True,
            "is_synthetic": self.is_synthetic,
            "synthetic_root": str(self.synthetic_root) if self.synthetic_root else None,
            "production_control": False,
        }
        write_durable_marker(self.marker_path, marker_data)
        self._owns_marker = True

    def start_or_restart_child(self, pct: int) -> None:
        """
        Configure TargetFanSpeeds in private state.json and launch/restart
        the native NBFC child process.
        """
        if not self.service_bin.is_file() or not os.access(self.service_bin, os.X_OK):
            raise RuntimeError(f"Guarded service executable is missing: {self.service_bin}")
        self.set_auto()
        if self.guard is None or self.ec_fd is None or not self._baseline_captured:
            raise RuntimeError("No verified EC baseline is available")
        if self.guard.capture_snapshot(self.ec_fd):
            raise RuntimeError("EC baseline changed before manual control")

        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        state_data = {"TargetFanSpeeds": [float(pct), float(pct)]}
        self.state_file.write_text(json.dumps(state_data))

        config_json_path = self.runtime_dir / "service.json"
        service_config = {
            "SelectedConfigId": str(self.profile_path),
            "EmbeddedControllerType": "ec_sys",
            "FanTemperatureSources": [
                {"FanIndex": idx, "TemperatureAlgorithmType": "Max", "Sensors": [str(self._cpu_path)]}
                for idx in range(2)
            ],
        }
        config_json_path.write_text(json.dumps(service_config, indent=2))

        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C",
               "PYTHONPATH": str(REPO_ROOT),
               "NBFC_EC_GUARD_CONFIG": str(self.policy_path),
               "NBFC_EC_GUARD_LOG": str(self.audit_log)}
        if self.is_synthetic:
            env["NBFC_EC_PATH"] = str(self.ec_path)
            env["NBFC_SYNTHETIC_ROOT"] = str(self.synthetic_root)

        cmd = [
            str(self.service_bin),
            "--debug",
            "--config-file",
            str(config_json_path),
            "--embedded-controller",
            "ec_sys",
        ]
        # The helper cannot exec NBFC until its PID and start time are durable.
        # EOF on the gate (including guardian SIGKILL) exits without EC writes.
        read_gate, write_gate = os.pipe()
        try:
            with (self.runtime_dir / 'native.log').open('ab', buffering=0) as output:
                self.child_process = subprocess.Popen(
                    [sys.executable, '-m', 'nitrofan.nbfc_guardian', '--exec-child', str(read_gate), *cmd],
                    env=env, start_new_session=True, pass_fds=(read_gate,),
                    stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                )
            self._child_identity = process_identity(self.child_process.pid)
            if not self._child_identity:
                raise RuntimeError("NBFC launch helper exited before ownership was recorded")
            self.write_marker()
            os.write(write_gate, b'1')
        finally:
            os.close(read_gate)
            os.close(write_gate)

    def terminate_child(self) -> None:
        """Gracefully stop NBFC child process with SIGTERM followed by SIGKILL."""
        if self.child_process is not None:
            if self.child_process.poll() is None:
                if not self._child_identity:
                    raise RuntimeError("NBFC ownership is unknown; refusing restoration")
                stop_process_group(self.child_process.pid, self._child_identity)
                self.child_process.wait(timeout=2.0)
            self.child_process = None
            self._child_identity = None

    def read_rpms(self) -> dict[str, int | None]:
        """Read 16-bit fan tachometer RPMs from EC registers 0x13 and 0x15."""
        if self.ec_fd is None:
            return {"fan1": None, "fan2": None}
        try:
            buf1 = os.pread(self.ec_fd, 2, 0x13)
            rpm1 = int.from_bytes(buf1, "little") if len(buf1) == 2 else None
        except OSError:
            rpm1 = None
        try:
            buf2 = os.pread(self.ec_fd, 2, 0x15)
            rpm2 = int.from_bytes(buf2, "little") if len(buf2) == 2 else None
        except OSError:
            rpm2 = None
        return {"fan1": rpm1, "fan2": rpm2}

    def check_sensors(self, sensor_provider: SystemSensorProvider) -> None:
        """
        Execute independent guardian safety checks:
        - CPU temperature loss (3 consecutive missing readings forces Auto)
        - Emergency high temperature (>= 90C forces 100%, cleared at <= 80C)
        - Fan stall / 0 RPM check
        """
        try:
            temps = {key: (float(value) if type(value) in (int, float) and
                           math.isfinite(value) and -50 <= value <= 150 else None)
                     for key, value in sensor_provider().items()}
        except Exception as e:
            logger.warning("Guardian sensor read failed: %s", e)
            temps = {}
        self.last_temps = temps

        # 1. CPU sensor loss check
        cpu_temp = temps.get("cpu")
        if cpu_temp is None:
            self.cpu_missing_count += 1
            if self.cpu_missing_count >= 3:
                self.fault = "CPU sensor unavailable; firmware Auto restored"
                self._requested_percent = None
                self.set_auto()
                return
        else:
            self.cpu_missing_count = 0

        # 2. Emergency temperature check (>= 90C -> emergency 100%, clears <= 80C)
        emergency_c = 90.0
        clear_c = 80.0
        hot_sources = {key for key, value in temps.items() if value is not None and value >= emergency_c}

        if hot_sources:
            if not self.emergency:
                self.emergency_trigger_temps = {}
            if not self.emergency or hot_sources - self._emergency_sources:
                logger.warning("Guardian emergency cooling triggered at %s; sampled temperatures: %s",
                               emergency_c, temps)
            for source in hot_sources:
                self.emergency_trigger_temps[source] = max(
                    temps[source], self.emergency_trigger_temps.get(source, temps[source]))
            self._emergency_sources.update(hot_sources)
            self.emergency = True
            if self.current_percent != 100:
                self.start_or_restart_child(100)
                self.current_percent = 100
        elif self.emergency:
            monitored = self._emergency_sources | {'cpu'}
            all_cool = all(temps.get(key) is not None and temps[key] <= clear_c for key in monitored)
            if all_cool:
                logger.info("Guardian detected cool temperatures (<= %.1fC); clearing emergency", clear_c)
                self.emergency = False
                self._emergency_sources.clear()
                if self._requested_percent is None:
                    self.set_auto()
                elif self.current_percent != self._requested_percent:
                    self.start_or_restart_child(self._requested_percent)
                    self.current_percent = self._requested_percent

        # 3. Fan 0 RPM stall check when active
        if self.current_percent is not None and self.current_percent >= 25:
            rpms = self.read_rpms()
            r1, r2 = rpms.get("fan1"), rpms.get("fan2")
            if r1 is None or r2 is None or r1 <= 0 or r2 <= 0:
                self.fault = "Fan tachometer missing or stopped; firmware Auto restored"
                self._requested_percent = None
                self.set_auto()

    def set_percent(self, pct: int) -> bool:
        """Set combined fan speed percent (25..100) across both fans."""
        if type(pct) is not int or not (25 <= pct <= 100):
            raise ValueError(f"Speed {pct}% out of allowed range 25..100")
        if self.fault or self.cpu_missing_count >= 3:
            raise RuntimeError(self.fault or "CPU sensor fault")
        self._requested_percent = pct
        effective = 100 if self.emergency else pct
        if self.current_percent != effective:
            self.start_or_restart_child(effective)
            self.current_percent = effective
        return True

    def set_auto(self) -> bool:
        """Return all fans to firmware automatic control."""
        self.terminate_child()
        if self._owns_marker and self.guard is not None and self.ec_fd is not None:
            res = self.guard.restore_snapshot(self.ec_fd)
            if res != 0 or self.guard.verify_snapshot(self.ec_fd) != 0:
                raise RuntimeError(f"Failed to restore or verify EC baseline ({res})")
            self.marker_path.unlink()
            self._owns_marker = False
        self.current_percent = None
        return True

    def cleanup(self) -> bool:
        """Restore firmware baseline, unregister marker, and close resources."""
        if self.cleaned_up:
            return not self._owns_marker
        self.cleaned_up = True
        logger.info("Guardian cleaning up...")

        auto_ok = False
        try:
            auto_ok = self.set_auto()
        except Exception:
            logger.exception("Firmware restoration failed; preserving the session marker")

        verify_ok = True
        if self.guard is not None:
            if self.ec_fd is not None and self._baseline_captured:
                try:
                    if self.guard.verify_snapshot(self.ec_fd) != 0:
                        verify_ok = False
                except Exception:
                    verify_ok = False
            self.guard.cleanup()
            self.guard = None

        if self.ec_fd is not None:
            try:
                os.close(self.ec_fd)
            except OSError:
                pass
            self.ec_fd = None

        if not auto_ok or not verify_ok:
            logger.error("Failed to restore or verify baseline; preserving durable session marker for recovery.")

        self.release_lock()
        return auto_ok and verify_ok

    def get_sensor_provider(self) -> SystemSensorProvider:
        """Resolve sensor provider handling synthetic and real sysfs layout."""
        hwmon_dir = self.sysfs_root / "class" / "hwmon"
        if not hwmon_dir.exists():
            hwmon_dir = self.sysfs_root / "sys" / "class" / "hwmon"
        if hwmon_dir.exists():
            return SystemSensorProvider(hwmon_root=hwmon_dir,
                                        pci_root=self.sysfs_root / 'bus/pci/devices')
        return SystemSensorProvider(sysfs_root=self.sysfs_root)

    def run(self) -> int:
        """Main guardian process loop."""
        def handle_signal(signum: int, frame: Any) -> None:
            self.stop_requested = True

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        try:
            self.preflight()
            self.init_ec_guard()
            sensor_provider = self.get_sensor_provider()
            first = sensor_provider()
            self._cpu_path = Path(sensor_provider._cpu_path) if sensor_provider._cpu_path else None
            if first.get('cpu') is None or self._cpu_path is None:
                raise RuntimeError("CPU Package temperature is unavailable")
            if any((rpm or 0) <= 0 for rpm in self.read_rpms().values()):
                raise RuntimeError("Both tachometers must be readable and spinning")

            # Signal ready to parent via stdout/pipe
            os.write(self.parent_out_fd, b'{"ready": true}\n')

            buffer = b""
            while not self.stop_requested:
                rlist, _, _ = select.select([self.parent_in_fd], [], [], 1.0)
                now = time.monotonic()

                if self.parent_in_fd in rlist:
                    chunk = os.read(self.parent_in_fd, 4096)
                    if not chunk:
                        # Parent closed pipe (EOF): immediate fail-safe stop!
                        logger.info("Parent EOF detected on pipe; restoring firmware baseline and exiting.")
                        break
                    buffer += chunk
                    if len(buffer) > FRAME_LIMIT:
                        raise RuntimeError("Guardian command buffer exceeded its limit")
                    self.last_parent_message_time = now

                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            req = json.loads(line.decode("utf-8"))
                            cmd = req.get("cmd")
                            if cmd == "ping":
                                os.write(self.parent_out_fd, b'{"ok": true}\n')
                            elif cmd == "set_auto":
                                self._requested_percent = None
                                ok = True if self.emergency else self.set_auto()
                                if self.cpu_missing_count == 0:
                                    self.fault = None
                                os.write(self.parent_out_fd, json.dumps({"ok": ok}).encode("utf-8") + b"\n")
                            elif cmd == "set_targets":
                                pct = req.get("percent")
                                ok = self.set_percent(pct)
                                os.write(
                                    self.parent_out_fd,
                                    json.dumps({"ok": ok, "percent": self.current_percent}).encode("utf-8") + b"\n",
                                )
                            elif cmd == "get_status":
                                rpms = self.read_rpms()
                                status = {
                                    "ok": True,
                                    "percent": self.current_percent,
                                    "emergency": self.emergency,
                                    "fault": self.fault,
                                    "rpms": rpms,
                                    "safety": {
                                        "temps": dict(self.last_temps),
                                        "trigger_temps": dict(self.emergency_trigger_temps),
                                        "emergency_sources": sorted(self._emergency_sources),
                                    },
                                }
                                os.write(self.parent_out_fd, json.dumps(status).encode("utf-8") + b"\n")
                            elif cmd == "close":
                                self.stop_requested = True
                                os.write(self.parent_out_fd, b'{"ok": true}\n')
                                break
                            else:
                                os.write(self.parent_out_fd, b'{"ok": false, "error": "unknown command"}\n')
                        except Exception as e:
                            err_msg = json.dumps({"ok": False, "error": str(e)}).encode("utf-8") + b"\n"
                            os.write(self.parent_out_fd, err_msg)

                if now - self.last_parent_message_time > PARENT_TIMEOUT_S:
                    logger.warning("Parent heartbeat timed out; restoring firmware control")
                    break

                # Safety checks
                if now - self._last_sensor_check >= 1.0:
                    self.check_sensors(sensor_provider)
                    self._last_sensor_check = now

                # Native child check
                if self.child_process is not None and self.current_percent is not None:
                    if self.child_process.poll() is not None:
                        logger.error(
                            "NBFC child process died unexpectedly (code=%s); restoring firmware Auto",
                            self.child_process.returncode,
                        )
                        try:
                            with (self.runtime_dir / 'native.log').open('rb') as native_log:
                                native_log.seek(0, os.SEEK_END)
                                native_log.seek(max(0, native_log.tell() - FRAME_LIMIT))
                                logger.error('Native NBFC log tail: %s',
                                             native_log.read(FRAME_LIMIT).decode('utf-8', errors='replace'))
                        except OSError as error:
                            logger.error('Native NBFC log unavailable: %s', error)
                        self.fault = "NBFC child exited; firmware Auto restored"
                        self._requested_percent = None
                        self.set_auto()

            return 0
        except Exception as e:
            logger.exception("Fatal error in NBFC guardian: %s", e)
            return 1
        finally:
            if not self.cleanup():
                raise RuntimeError("Guardian cleanup failed; external recovery is required")


def run_recovery(
    config_path: str | Path | None = None,
    runtime_dir: str | Path | None = None,
    manifest_path: str | Path | None = None,
    policy_path: str | Path | None = None,
    synthetic_root: str | Path | None = None,
    ec_path: str | Path | None = None,
    sysfs_root: str | Path | None = None,
) -> int:
    """Restore an owned session after all writers have stopped, or do no writes."""
    guardian = None
    try:
        config = load_config(config_path) if config_path is not None else None
        guardian = NBFCGuardian(config=config, runtime_dir=runtime_dir,
                                manifest_path=manifest_path, policy_path=policy_path,
                                synthetic_root=synthetic_root, ec_path=ec_path,
                                sysfs_root=sysfs_root)
        marker = guardian.marker_path
        if not marker.exists() and not marker.is_symlink():
            return 0
        guardian.preflight(recovering=True)
        descriptor = os.open(marker, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or
                    metadata.st_mode & 0o077 or metadata.st_size > FRAME_LIMIT):
                raise PermissionError("Recovery record ownership, mode or size is invalid")
        finally:
            os.close(descriptor)
        record = strict_json(marker)
        if (record.get('version') != 1 or record.get('active') is not True or
                record.get('baseline_captured') is not True or
                record.get('is_synthetic') is not guardian.is_synthetic or
                record.get('ec_path') != str(guardian.ec_path)):
            raise ValueError("Recovery record has invalid session identity")
        for key, artifact in (('manifest_sha256', guardian.manifest_path),
                              ('policy_sha256', guardian.policy_path),
                              ('profile_sha256', guardian.profile_path)):
            if record.get(key) != compute_file_sha256(artifact):
                raise ValueError(f"Recovery record does not match {key}")
        if record.get('boot_id') != get_boot_id(guardian.synthetic_root):
            # A new boot has no surviving writer. Require a matching baseline
            # before retiring the stale record, with no restoration writes.
            guardian.init_ec_guard()
            marker.unlink()
            return 0
        pid, identity = record.get('nbfc_pid'), record.get('nbfc_identity')
        if pid is not None:
            if type(pid) is not int or pid <= 1 or not isinstance(identity, str):
                raise ValueError("Recovery record has invalid NBFC process identity")
            stop_process_group(pid, identity)
        guardian.guard = ECGuardBinding(guardian.library_path)
        if guardian.guard.init(str(guardian.policy_path), str(guardian.audit_log)):
            raise RuntimeError("Recovery guard initialization failed")
        guardian.ec_fd = os.open(guardian.ec_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        if guardian.guard.recover_from_baselines(guardian.ec_fd):
            raise RuntimeError("Recovery guard rejected the current EC state")
        if guardian.guard.verify_snapshot(guardian.ec_fd):
            raise RuntimeError("Restored EC baseline did not verify")
        marker.unlink()
        logger.info("External recovery restored and verified firmware control")
        return 0
    except Exception:
        logger.exception("Recovery failed; retaining the session record")
        return 1
    finally:
        if guardian is not None:
            if guardian.ec_fd is not None:
                os.close(guardian.ec_fd)
            if guardian.guard is not None:
                guardian.guard.cleanup()
            guardian.release_lock()


def main() -> None:
    if len(sys.argv) >= 4 and sys.argv[1] == '--exec-child':
        gate = int(sys.argv[2])
        permission = os.read(gate, 1)
        os.close(gate)
        if permission != b'1':
            raise SystemExit(1)
        os.execve(sys.argv[3], sys.argv[3:], os.environ)
    parser = argparse.ArgumentParser(
        description="NitroFan NBFC Guardian Process & Recovery"
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Run ExecStopPost recovery to restore baseline registers after crash/SIGKILL",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to TOML configuration file",
    )
    parser.add_argument(
        "--runtime-dir",
        default=None,
        help="Private runtime directory for session marker, locks, and logs",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Path to build manifest JSON",
    )
    parser.add_argument(
        "--policy",
        default=None,
        help="Path to EC guard policy JSON",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Path to NBFC profile JSON",
    )
    parser.add_argument('--service-bin', default=None)
    parser.add_argument(
        "--ec-path",
        default=None,
        help="Path to EC device or synthetic file",
    )
    parser.add_argument(
        "--sysfs-root",
        default=None,
        help="Root path for sysfs sensors",
    )
    parser.add_argument(
        "--synthetic-root",
        default=None,
        help="Synthetic sandbox root for testing",
    )
    parser.add_argument(
        "--guardian",
        action="store_true",
        help="Run as background guardian process",
    )
    parser.add_argument(
        "--parent-in-fd",
        type=int,
        default=0,
        help="File descriptor for reading commands from parent",
    )
    parser.add_argument(
        "--parent-out-fd",
        type=int,
        default=1,
        help="File descriptor for writing replies to parent",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.recover:
        code = run_recovery(
            config_path=args.config,
            runtime_dir=args.runtime_dir,
            manifest_path=args.manifest,
            policy_path=args.policy,
            synthetic_root=args.synthetic_root,
            ec_path=args.ec_path,
            sysfs_root=args.sysfs_root,
        )
        sys.exit(code)

    config = None
    if args.config:
        try:
            config = load_config(args.config)
        except Exception as e:
            sys.stderr.write(f"Config error: {e}\n")
            sys.exit(1)

    guardian = NBFCGuardian(
        config=config,
        runtime_dir=args.runtime_dir,
        manifest_path=args.manifest,
        policy_path=args.policy,
        profile_path=args.profile,
        service_bin=args.service_bin,
        ec_path=args.ec_path,
        sysfs_root=args.sysfs_root,
        synthetic_root=args.synthetic_root,
        parent_in_fd=args.parent_in_fd,
        parent_out_fd=args.parent_out_fd,
    )
    sys.exit(guardian.run())


if __name__ == "__main__":
    main()
