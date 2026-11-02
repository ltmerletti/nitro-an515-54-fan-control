"""Bounded guarded maximum-only EC trial runner for Acer Nitro AN515-54."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional
import uuid

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_POLICY_PATH = REPO_ROOT / "tools" / "ec_guard" / "config" / "ec_guard_policy.json"
DEFAULT_PROFILE_PATH = REPO_ROOT / "tools" / "ec_guard" / "profiles" / "Acer_Nitro_AN515-54_guarded_max.json"
DEFAULT_MANIFEST_PATH = REPO_ROOT / "tools" / "ec_guard" / "guarded_nbfc_manifest.json"

EC_GUARD_FORBIDDEN_REGISTER = 0x03

logger = logging.getLogger("nbfc_guarded_trial")


def compute_file_sha256(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    hasher = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class ECGuardBinding:
    """Wrapper around libnbfc_ec_guard compiled C library via ctypes."""

    def __init__(self, library_path: Optional[Path] = None) -> None:
        if library_path is None:
            lib_name = "libnbfc_ec_guard.dylib" if platform.system() == "Darwin" else "libnbfc_ec_guard.so"
            candidate_paths = [
                REPO_ROOT / "tools" / "ec_guard" / lib_name,
                Path("/home/luke/nbfc-evaluation/guarded-bin") / lib_name,
                Path("/home/luke/nbfc-evaluation/ec_guard") / lib_name,
                Path("/usr/local/lib") / lib_name,
            ]
            for candidate in candidate_paths:
                if candidate.exists():
                    library_path = candidate
                    break

        if library_path is None or not library_path.exists():
            raise FileNotFoundError(
                "libnbfc_ec_guard library not found. Build it with 'make -C tools/ec_guard'."
            )

        self._lib = ctypes.CDLL(str(library_path))
        self._bind_functions()

    def _bind_functions(self) -> None:
        self._lib.ec_guard_init.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        self._lib.ec_guard_init.restype = ctypes.c_int

        self._lib.ec_guard_capture_snapshot.argtypes = [ctypes.c_int]
        self._lib.ec_guard_capture_snapshot.restype = ctypes.c_int

        self._lib.ec_guard_pwrite.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_longlong,
            ctypes.c_char_p,
        ]
        self._lib.ec_guard_pwrite.restype = ctypes.c_int

        self._lib.ec_guard_write_byte.argtypes = [
            ctypes.c_int,
            ctypes.c_uint8,
            ctypes.c_uint8,
            ctypes.c_char_p,
        ]
        self._lib.ec_guard_write_byte.restype = ctypes.c_int

        self._lib.ec_guard_write_word.argtypes = [
            ctypes.c_int,
            ctypes.c_uint8,
            ctypes.c_uint16,
            ctypes.c_char_p,
        ]
        self._lib.ec_guard_write_word.restype = ctypes.c_int

        self._lib.ec_guard_restore_snapshot.argtypes = [ctypes.c_int]
        self._lib.ec_guard_restore_snapshot.restype = ctypes.c_int

        self._lib.ec_guard_verify_snapshot.argtypes = [ctypes.c_int]
        self._lib.ec_guard_verify_snapshot.restype = ctypes.c_int

        if hasattr(self._lib, "ec_guard_recover_from_baselines"):
            self._lib.ec_guard_recover_from_baselines.argtypes = [ctypes.c_int]
            self._lib.ec_guard_recover_from_baselines.restype = ctypes.c_int

        self._lib.ec_guard_cleanup.argtypes = []
        self._lib.ec_guard_cleanup.restype = None

    def init(self, config_path: str, log_path: str) -> int:
        return self._lib.ec_guard_init(
            config_path.encode("utf-8"),
            log_path.encode("utf-8"),
        )

    def capture_snapshot(self, file_descriptor: int) -> int:
        return self._lib.ec_guard_capture_snapshot(file_descriptor)

    def pwrite(
        self,
        file_descriptor: int,
        data: bytes,
        offset: int,
        context: str,
    ) -> int:
        return self._lib.ec_guard_pwrite(
            file_descriptor,
            data,
            len(data),
            offset,
            context.encode("utf-8"),
        )

    def write_byte(
        self,
        file_descriptor: int,
        offset: int,
        value: int,
        context: str,
    ) -> int:
        return self._lib.ec_guard_write_byte(
            file_descriptor,
            offset,
            value,
            context.encode("utf-8"),
        )

    def write_word(
        self,
        file_descriptor: int,
        offset: int,
        value: int,
        context: str,
    ) -> int:
        return self._lib.ec_guard_write_word(
            file_descriptor,
            offset,
            value,
            context.encode("utf-8"),
        )

    def restore_snapshot(self, file_descriptor: int) -> int:
        return self._lib.ec_guard_restore_snapshot(file_descriptor)

    def recover_from_baselines(self, file_descriptor: int) -> int:
        if not hasattr(self._lib, "ec_guard_recover_from_baselines"):
            raise NotImplementedError("libnbfc_ec_guard does not export ec_guard_recover_from_baselines")
        return self._lib.ec_guard_recover_from_baselines(file_descriptor)

    def verify_snapshot(self, file_descriptor: int) -> int:
        return self._lib.ec_guard_verify_snapshot(file_descriptor)

    def cleanup(self) -> None:
        self._lib.ec_guard_cleanup()


def strict_json(path: Path) -> dict:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(path.read_text(), object_pairs_hook=object_pairs,
                       parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


class GuardedTrialSupervisor:
    """Supervise one foreground NBFC child, then restore its saved EC baseline."""

    def __init__(self, duration_s=30, ec_path=None, policy_path=None,
                 profile_path=None, service_bin=None, lib_dir=None,
                 manifest_path=DEFAULT_MANIFEST_PATH, audit_log=None, telemetry_log=None,
                 sysfs_root=None, state_file=None, synthetic_root=None, library_path=None,
                 verify_build=False, dmi_root=None):
        self.duration_s = duration_s
        self._policy_explicit = policy_path is not None
        self._profile_explicit = profile_path is not None
        self.policy_path = Path(policy_path) if policy_path is not None else DEFAULT_POLICY_PATH
        self.profile_path = Path(profile_path) if profile_path is not None else DEFAULT_PROFILE_PATH
        self.manifest_path = Path(manifest_path)
        self.synthetic_root = Path(synthetic_root).resolve() if synthetic_root else None
        self.is_synthetic = self.synthetic_root is not None
        self.runtime_root = self.synthetic_root
        self.ec_path = str(ec_path or (self.synthetic_root / "fake_ec.bin"
                                      if self.is_synthetic else "/sys/kernel/debug/ec/ec0/io"))
        self.sysfs_root = Path(sysfs_root) if sysfs_root else (
            self.synthetic_root / "sys" if self.is_synthetic else Path("/sys"))
        self.dmi_root = Path(dmi_root) if dmi_root else (
            self.synthetic_root / "sys/class/dmi/id" if self.is_synthetic else Path("/sys/class/dmi/id"))
        self.service_bin = Path(service_bin) if service_bin else None
        self.lib_dir = Path(lib_dir) if lib_dir else None
        self.library_path = Path(library_path) if library_path else None
        self.audit_log = Path(audit_log) if audit_log else (
            self.synthetic_root / "ec_guard_audit.log" if self.is_synthetic else None)
        self.telemetry_log = Path(telemetry_log) if telemetry_log else (
            self.synthetic_root / "trial_telemetry.jsonl" if self.is_synthetic else None)
        self.state_file = Path(state_file) if state_file else (
            self.synthetic_root / "state.json" if self.is_synthetic else None)
        self.lock_file_path = (self.synthetic_root / "nitrofan_guarded_trial.lock"
                               if self.is_synthetic else Path("/run/lock/nitrofan_guarded_trial.lock"))
        self.ec_fd = None
        self.lock_fd = None
        self.child_process = None
        self.guard = None
        self._stop_requested = False
        self._signal_number = None
        self._baseline_captured = False
        self._child_launched = False
        self._state_prepared = False
        self._saved_state_backup = None
        self._state_originally_existed = False
        self._cleanup_done = False
        self._cleanup_result = True
        self._cpu_path = None
        self._state_explicit = state_file is not None
        self._verify_build = verify_build
        self._restored = False

    def acquire_trial_lock(self):
        if self.lock_fd is not None:
            return
        self.lock_file_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_file_path,
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("Trial lock must be a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(descriptor)
            raise RuntimeError("Another trial is active, or the trial lock is unsafe")
        self.lock_fd = descriptor

    def _inside(self, path):
        if not Path(path).resolve().is_relative_to(self.synthetic_root):
            raise PermissionError(f"Synthetic path escapes sandbox: {path}")

    def _attest_build(self):
        manifest = strict_json(self.manifest_path)
        if manifest.get("upstream_commit") != "9da010d6a2e8351bdf6900d6c3643f1e742af936":
            raise ValueError("Missing or incorrect pinned upstream commit")
        files = {
            "service_binary": self.service_bin or Path(manifest["service_binary_path"]),
            "shared_library": self.library_path or Path(manifest["shared_library_path"]),
            "ec_guard_c": REPO_ROOT / "tools/ec_guard/ec_guard.c",
            "ec_guard_h": REPO_ROOT / "tools/ec_guard/ec_guard.h",
            "patch": REPO_ROOT / "tools/ec_guard/patches/nbfc-0.5.4-guarded-ec-sys.patch",
            "policy": self.policy_path,
            "profile": self.profile_path,
        }
        for name, path in files.items():
            expected = manifest.get(name + "_sha256")
            if not isinstance(expected, str) or len(expected) != 64 or compute_file_sha256(path) != expected:
                raise ValueError(f"Guarded build digest is missing or mismatched: {name}")
        self.service_bin = files["service_binary"]
        self.library_path = files["shared_library"]
        self.runtime_root = Path(manifest["runtime_root"]).resolve(strict=True)
        if self.runtime_root == Path("/") or str(self.runtime_root).startswith(("/etc", "/run", "/var")):
            raise ValueError("The trial must use a private runtime directory")
        expected_state = self.runtime_root / "var/lib/nbfc/state.json"
        if self._state_explicit and self.state_file.resolve() != expected_state.resolve():
            raise ValueError("State file differs from the private compiled-in state path")
        self.state_file = expected_state

    def _validate_profile(self):
        policy = strict_json(self.policy_path)
        profile = strict_json(self.profile_path)
        rules = policy.get("allowed_writes", [])
        allowed = {}
        for rule in rules:
            offset, values = rule.get("offset"), rule.get("allowed_values")
            if type(offset) is not int or offset in allowed or not 0 <= offset < 256 or offset == 3:
                raise ValueError("Invalid or repeated allowlist offset")
            if not isinstance(values, list) or not values or any(
                    type(value) is not int or not 0 <= value <= 255 for value in values):
                raise ValueError("Invalid allowlist values")
            allowed[offset] = values
        fans = profile.get("FanConfigurations", [])
        if len(fans) != 2 or profile.get("ReadWriteWords") is not True:
            raise ValueError("Trial profile requires exactly two word-based fans")
        for fan in fans:
            target = fan["WriteRegister"]
            if allowed.get(target) != [100] or allowed.get(target + 1) != [0]:
                raise ValueError("Active speed writes must permit exactly the 100% word")
            if fan.get("MinSpeedValue") != 0 or fan.get("MaxSpeedValue") != 100:
                raise ValueError("Unexpected fan speed scaling")
            if fan.get("FanSpeedResetValue") != 100 or not fan.get("TemperatureThresholds"):
                raise ValueError("Reset and curve speeds must stay at maximum")
            if any(point.get("FanSpeed") != 100 for point in fan["TemperatureThresholds"]):
                raise ValueError("All trial curve points must request 100%")
        for register in profile.get("RegisterWriteConfigurations", []):
            if (register.get("WriteMode") != "Set" or register.get("ResetWriteMode") != "Set" or
                    register.get("Value") not in allowed.get(register.get("Register"), []) or
                    register.get("ResetValue") not in allowed.get(register.get("Register"), [])):
                raise ValueError("Initialization/reset writes do not match the active policy")
        if "Lua" in json.dumps(profile) or "Acpi" in json.dumps(profile):
            raise ValueError("Executable Lua and ACPI methods are outside this trial")
        self._profile = profile

    def _verify_hardware_identity(self):
        from nitrofan.models import detect_model, load_catalog

        catalog = load_catalog()
        match = detect_model(catalog=catalog, dmi_root=self.dmi_root)
        if not match.matched:
            vendor = match.identity.sys_vendor or "<unknown>"
            product = match.identity.product_name or "<unknown>"
            raise RuntimeError(
                f"Hardware identity differs from the model catalogue: {vendor} {product}"
            )
        if not match.capabilities.guarded_max_trial:
            vendor = match.identity.sys_vendor or "<unknown>"
            product = match.identity.product_name or "<unknown>"
            bios = match.identity.bios_version or "<unknown>"
            raise RuntimeError(
                f"Hardware {vendor} {product} (BIOS {bios}) is not eligible for guarded trial: "
                f"{match.support_status}. {match.next_steps}"
            )
        artifacts = match.model_entry.local_artifacts
        for attribute, configured, explicit in (
                ("policy_path", artifacts.policy_path, self._policy_explicit),
                ("profile_path", artifacts.guarded_profile_path, self._profile_explicit)):
            selected = (REPO_ROOT / configured).resolve()
            if not selected.is_relative_to(REPO_ROOT.resolve()) or not selected.is_file():
                raise RuntimeError(f"Selected model artifact is missing or outside the repository: {configured}")
            if explicit and getattr(self, attribute).resolve() != selected:
                raise ValueError(f"Explicit {attribute} differs from the selected model catalogue entry")
            setattr(self, attribute, selected)
        return match

    def preflight_checks(self, write_enabled=True):
        if type(self.duration_s) is not int or not 1 <= self.duration_s <= 180:
            raise ValueError("Duration must be an integer from 1 to 180 seconds")
        if self.is_synthetic:
            self._inside(self.ec_path)
            information = Path(self.ec_path).lstat()
            if not stat.S_ISREG(information.st_mode) or information.st_size != 256:
                raise ValueError("Synthetic EC must be a regular 256-byte file")
            for path in (self.sysfs_root, self.state_file, self.audit_log, self.telemetry_log):
                self._inside(path)
            if self.service_bin is not None:
                self._inside(self.service_bin)
            if self._verify_build:
                self._attest_build()
                for path in (self.runtime_root, self.state_file, self.service_bin):
                    self._inside(path)
        else:
            if os.geteuid() != 0:
                raise PermissionError("Real hardware trial requires sudo")
            if self.ec_path != "/sys/kernel/debug/ec/ec0/io" or self.sysfs_root != Path("/sys"):
                raise ValueError("Real mode must use the verified EC and sysfs paths")
            if self.dmi_root != Path("/sys/class/dmi/id"):
                raise ValueError("Real mode must identify hardware through the system DMI path")
            self._verify_hardware_identity()
            expected = ("Y", "1") if write_enabled else ("N", "0")
            if Path("/sys/module/ec_sys/parameters/write_support").read_text().strip() not in expected:
                raise RuntimeError("Unexpected EC write support for this test stage")
            self._attest_build()
            check = subprocess.run(["pgrep", "-x", "nbfc_service"], capture_output=True, timeout=3)
            if check.returncode != 1:
                raise RuntimeError("An NBFC process exists, or the process check failed")
        self._validate_profile()
        self.acquire_trial_lock()
        self.verify_sensors_available()
        if self.service_bin is not None and (
                not self.service_bin.is_file() or not os.access(self.service_bin, os.X_OK)):
            raise ValueError("Guarded service executable is missing")
        if not self.is_synthetic:
            directory = Path(tempfile.mkdtemp(prefix="trial-", dir=self.runtime_root / "logs"))
            self.audit_log = directory / "writes.jsonl"
            self.telemetry_log = directory / "telemetry.jsonl"

    def isolate_existing_state(self):
        if self._state_prepared:
            return
        self._state_originally_existed = self.state_file.exists()
        if self._state_originally_existed:
            if self.state_file.is_symlink():
                raise ValueError("Trial state file cannot be a symlink")
            backup = self.state_file.with_name("state.backup-" + uuid.uuid4().hex)
            shutil.copy2(self.state_file, backup)
            self._saved_state_backup = backup
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_prepared = True
        self.state_file.write_text(json.dumps({"TargetFanSpeeds": [100.0, 100.0]}))

    def restore_isolated_state(self):
        if not self._state_prepared:
            return
        if self._state_originally_existed:
            self._saved_state_backup.replace(self.state_file)
            self._saved_state_backup = None
        else:
            self.state_file.unlink(missing_ok=True)
        self._state_prepared = False

    def open_ec(self, read_only=False):
        self.guard = ECGuardBinding(self.library_path)
        status = self.guard.init(str(self.policy_path), str(self.audit_log))
        if status:
            raise RuntimeError(f"Guard initialization failed ({status})")
        access = os.O_RDONLY if read_only else os.O_RDWR
        self.ec_fd = os.open(self.ec_path, access | os.O_NOFOLLOW | os.O_CLOEXEC)

    def capture_and_verify_baseline(self):
        if self.guard.capture_snapshot(self.ec_fd):
            raise RuntimeError("EC baseline differs from the active configuration")
        self._baseline_captured = True

    def _sensor_devices(self, name):
        root = self.sysfs_root / "class/hwmon"
        try:
            devices = sorted(root.iterdir())
        except OSError:
            return
        for device in devices:
            try:
                if (device / "name").read_text().strip() == name:
                    yield device
            except OSError:
                continue

    def read_cpu_package_temp(self):
        self._cpu_path = None
        for device in self._sensor_devices("coretemp"):
            for label in sorted(device.glob("temp*_label")):
                try:
                    if label.read_text().strip() == "Package id 0":
                        source = label.with_name(label.name.replace("_label", "_input"))
                        value = int(source.read_text()) / 1000
                        if math.isfinite(value):
                            self._cpu_path = source
                            return value
                except (OSError, ValueError):
                    continue
        return None

    def read_tachometer_rpms(self):
        result = {}
        for device in self._sensor_devices("acer"):
            for fan in ("fan1", "fan2"):
                try:
                    value = int((device / (fan + "_input")).read_text())
                    if value >= 0:
                        result[fan] = value
                except (OSError, ValueError):
                    continue
            break
        return result

    def verify_sensors_available(self):
        temperature = self.read_cpu_package_temp()
        if temperature is None or not 0 < temperature <= 105:
            raise RuntimeError(f"CPU package temperature unavailable or invalid: {temperature}")
        fans = self.read_tachometer_rpms()
        if any(fans.get(fan, 0) <= 0 for fan in ("fan1", "fan2")):
            raise RuntimeError(f"Both fan tachometers must be readable and spinning: {fans}")

    def log_telemetry_sample(self, phase, elapsed_s):
        temperature, fans = self.read_cpu_package_temp(), self.read_tachometer_rpms()
        with self.telemetry_log.open("a") as output:
            output.write(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                     "elapsed_s": elapsed_s, "phase": phase,
                                     "cpu_package_temp": temperature, "tachometers": fans}) + "\n")
        return temperature, fans

    def run_watchdog_loop(self):
        start = time.monotonic()
        while not self._stop_requested:
            elapsed = time.monotonic() - start
            # Child health is checked before the deadline, including at the last tick.
            if self.child_process is None or self.child_process.poll() is not None:
                logger.error("NBFC exited before the requested test duration")
                return False
            temperature, fans = self.log_telemetry_sample("active_trial", elapsed)
            if temperature is None or not 0 < temperature <= 105:
                return False
            # At high valid temperatures this trial already requests 100%.
            if elapsed >= 3 and any(fans.get(fan, 0) <= 0 for fan in ("fan1", "fan2")):
                return False
            if elapsed >= self.duration_s:
                return True
            time.sleep(min(1.0, self.duration_s - elapsed))
        return False

    def terminate_child(self):
        child = self.child_process
        if child is None:
            return True
        # Always signal the owned process group, even if its leader has exited.
        for signum, timeout in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                child.wait(timeout=timeout)
                return True
            except OSError:
                return False
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                continue
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                return True
            # A descendant still exists. Escalate before any baseline restoration.
        return False

    def restore_and_verify(self):
        if self._restored:
            return True
        if not self._baseline_captured or not self._child_launched:
            return True
        restored = self.guard.restore_snapshot(self.ec_fd)
        verified = self.guard.verify_snapshot(self.ec_fd)
        if restored or verified:
            logger.error("EC restoration failed: restore=%s verify=%s", restored, verified)
            return False
        logger.info("Saved EC bytes restored and verified. Firmware behavior still needs observation.")
        self._restored = True
        return True

    def post_stop_monitoring(self, duration_s=5):
        start = time.monotonic()
        while time.monotonic() - start < duration_s:
            self.log_telemetry_sample("post_stop_monitoring", time.monotonic() - start)
            if self._stop_requested:
                break
            time.sleep(1)

    def cleanup(self):
        if self._cleanup_done:
            return self._cleanup_result
        success = True
        stopped = False
        try:
            stopped = self.terminate_child()
            if not stopped:
                logger.error("Child group could not be stopped; refusing to race its EC writes")
                success = False
            elif self.ec_fd is not None:
                success = self.restore_and_verify()
        except Exception:
            logger.exception("Child stop or EC restoration raised an error")
            success = False
        finally:
            if self.guard:
                self.guard.cleanup()
            if self.ec_fd is not None:
                os.close(self.ec_fd)
                self.ec_fd = None
            try:
                if stopped:
                    self.restore_isolated_state()
            except Exception:
                logger.exception("Could not restore the private NBFC state file")
                success = False
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
        self._cleanup_result = success
        self._cleanup_done = True
        return success

    def run(self):
        previous_handlers = {}
        def request_stop(signum, _frame):
            self._signal_number = signum
            self._stop_requested = True
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, request_stop)
        result = 1
        try:
            self.preflight_checks()
            if self.service_bin is None:
                raise ValueError("A guarded NBFC child is required for a trial")
            if self._stop_requested:
                raise InterruptedError("Stop requested during preflight")
            self.open_ec()
            self.capture_and_verify_baseline()
            self.isolate_existing_state()
            config_path = self.audit_log.parent / "service.json"
            config = {"SelectedConfigId": str(self.profile_path.resolve()),
                      "EmbeddedControllerType": "ec_sys",
                      "FanTemperatureSources": [
                          {"FanIndex": index, "TemperatureAlgorithmType": "Max",
                           "Sensors": [str(self._cpu_path.resolve())]} for index in range(2)]}
            config_path.write_text(json.dumps(config, indent=2))
            environment = os.environ.copy()
            for key in ("NBFC_EC_PATH", "NBFC_EC_GUARD_CONFIG", "NBFC_EC_GUARD_LOG",
                        "LD_PRELOAD", "LD_LIBRARY_PATH"):
                environment.pop(key, None)
            if self.lib_dir:
                environment["LD_LIBRARY_PATH"] = str(self.lib_dir)
            environment["NBFC_EC_GUARD_CONFIG"] = str(self.policy_path.resolve())
            environment["NBFC_EC_GUARD_LOG"] = str(self.audit_log.resolve())
            if self.is_synthetic:
                environment["NBFC_EC_PATH"] = str(Path(self.ec_path).resolve())
                environment["NBFC_SYNTHETIC_ROOT"] = str(self.synthetic_root)
            if self._stop_requested:
                raise InterruptedError("Stop requested before starting NBFC")
            command = [str(self.service_bin), "--debug", "--config-file", str(config_path),
                       "--embedded-controller", "ec_sys"]
            logger.info("Starting %ss maximum-only trial; logs: %s", self.duration_s, self.audit_log.parent)
            self.child_process = subprocess.Popen(command, env=environment, start_new_session=True,
                                                  stdin=subprocess.DEVNULL)
            self._child_launched = True
            if self.run_watchdog_loop() and self.terminate_child() and self.restore_and_verify():
                self.post_stop_monitoring()
                result = 0
        except Exception:
            logger.exception("Guarded trial failed")
        finally:
            if not self.cleanup():
                result = 1
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        return 128 + self._signal_number if self._signal_number else result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--synthetic-root", type=Path)
    parser.add_argument("--ec-path")
    parser.add_argument("--sysfs-root", type=Path)
    parser.add_argument("--service-bin", type=Path)
    parser.add_argument("--lib-dir", type=Path)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--dmi-root", type=Path, default=None,
                        help="Optional custom sysfs DMI path for hardware identity checking")
    parser.add_argument("--check-only", action="store_true",
                        help="Check provenance, sensors and EC baseline with writes still disabled")
    parser.add_argument("--verify-build", action="store_true",
                        help="Also require build provenance during a synthetic integration test")
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    supervisor = GuardedTrialSupervisor(
        duration_s=options.duration, synthetic_root=options.synthetic_root,
        ec_path=options.ec_path, sysfs_root=options.sysfs_root,
        service_bin=options.service_bin, lib_dir=options.lib_dir, library_path=options.library,
        manifest_path=options.manifest, policy_path=options.policy, profile_path=options.profile,
        verify_build=options.verify_build, dmi_root=options.dmi_root)
    if options.check_only:
        result = 1
        try:
            supervisor.preflight_checks(write_enabled=False)
            supervisor.open_ec(read_only=True)
            supervisor.capture_and_verify_baseline()
            logger.info("Read-only preflight passed. No control writes performed.")
            result = 0
        except Exception:
            logger.exception("Read-only preflight failed")
        finally:
            if not supervisor.cleanup():
                result = 1
        raise SystemExit(result)
    result = supervisor.run()
    # The caller's shell can redirect this into a user-owned log for SSH retrieval.
    for label, path in (("EC write audit", supervisor.audit_log),
                        ("Temperature and RPM samples", supervisor.telemetry_log)):
        if path is not None and path.is_file():
            print(f"=== {label}: {path} ===", flush=True)
            print(path.read_text(), flush=True)
    raise SystemExit(result)


if __name__ == "__main__":
    main()


