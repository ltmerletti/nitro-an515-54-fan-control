"""
sensors.py - Real hardware temperature sensor provider for NitroFan.

Discovers and polls Linux sysfs hwmon and PCI devices:
- Canonical CPU temperature from coretemp (Package id 0 or temp1_input)
- Awake-only targeted NVIDIA GPU temperature via nvidia-smi
- Neutral Acer temperatures from acer_wmi for correlation

Any read or hardware failure returns None, never a fake zero.
"""

from __future__ import annotations

import logging
import math
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

NVIDIA_VENDOR_ID = "0x10de"
NVIDIA_DISPLAY_CLASS_PREFIX = "0x03"


def _read_sysfs(path: str | Path) -> str | None:
    """Read stripped text from a sysfs file, or None on any error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _parse_millidegrees(path: str | Path) -> float | None:
    """
    Read millidegrees Celsius from a sysfs temp*_input file.

    Returns finite float in degrees Celsius, or None on invalid/error.
    """
    raw = _read_sysfs(path)
    if raw is None or not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        return None
    try:
        temp_c = val / 1000.0
    except OverflowError:
        return None
    if not math.isfinite(temp_c) or temp_c < -50.0 or temp_c > 150.0:
        return None
    return round(temp_c, 2)


class SystemSensorProvider:
    """
    Real temperature provider polling sysfs hwmon and NVIDIA GPU.

    Supports dependency injection of sysfs/hwmon/pci roots and subprocess
    runner for deterministic testing against synthetic trees.
    """

    def __init__(
        self,
        sysfs_root: str | Path = "",
        hwmon_root: str | Path | None = None,
        pci_root: str | Path | None = None,
        subprocess_runner: Callable[..., Any] = subprocess.run,
        nvidia_smi_timeout: float = 3.0,
    ) -> None:
        self.sysfs_root = str(sysfs_root)
        if hwmon_root is not None:
            self.hwmon_root = str(hwmon_root)
        elif self.sysfs_root:
            self.hwmon_root = os.path.join(self.sysfs_root, "sys", "class", "hwmon")
        else:
            self.hwmon_root = "/sys/class/hwmon"

        if pci_root is not None:
            self.pci_root = str(pci_root)
        elif self.sysfs_root:
            self.pci_root = os.path.join(self.sysfs_root, "sys", "bus", "pci", "devices")
        else:
            self.pci_root = "/sys/bus/pci/devices"

        self.subprocess_runner = subprocess_runner
        self.nvidia_smi_timeout = nvidia_smi_timeout

        # Discovered paths
        self._cpu_path: str | None = None
        self._acer_temp_paths: dict[str, str] = {}
        self._nvidia_pci_id: str | None = None
        self._nvidia_runtime_path: str | None = None

        self.rediscover()

    def rediscover(self) -> None:
        """
        Scan hwmon and PCI directories non-recursively to discover sensors.

        Handles dynamic re-enumeration across driver reloads or resume.
        """
        self._cpu_path = None
        self._acer_temp_paths.clear()
        self._nvidia_pci_id = None
        self._nvidia_runtime_path = None

        # 1. Non-recursive scan of hwmon directory
        if os.path.isdir(self.hwmon_root):
            try:
                entries = sorted(os.listdir(self.hwmon_root))
            except OSError:
                entries = []

            for entry in entries:
                hwmon_dir = os.path.join(self.hwmon_root, entry)
                if not os.path.isdir(hwmon_dir):
                    continue
                name = _read_sysfs(os.path.join(hwmon_dir, "name"))
                if not name:
                    continue

                if name == "coretemp" and self._cpu_path is None:
                    try:
                        dir_files = os.listdir(hwmon_dir)
                    except OSError:
                        dir_files = []

                    # Look for Package id 0 label first
                    pkg_input: str | None = None
                    for f in dir_files:
                        if f.startswith("temp") and f.endswith("_label"):
                            lbl = _read_sysfs(os.path.join(hwmon_dir, f))
                            if lbl == "Package id 0":
                                base = f[:-len("_label")]
                                candidate = os.path.join(hwmon_dir, f"{base}_input")
                                if os.path.isfile(candidate):
                                    pkg_input = candidate
                                    break
                    if pkg_input:
                        self._cpu_path = pkg_input
                    else:
                        # Fallback to temp1_input
                        fallback = os.path.join(hwmon_dir, "temp1_input")
                        if os.path.isfile(fallback):
                            self._cpu_path = fallback

                elif name == "acer":
                    try:
                        dir_files = sorted(os.listdir(hwmon_dir))
                    except OSError:
                        dir_files = []

                    temp_pattern = re.compile(r"^temp(\d+)_input$")
                    for f in dir_files:
                        m = temp_pattern.match(f)
                        if m:
                            key = f"acer_temp{m.group(1)}"
                            self._acer_temp_paths[key] = os.path.join(hwmon_dir, f)

        # 2. Non-recursive scan of PCI devices for NVIDIA display GPU
        if os.path.isdir(self.pci_root):
            try:
                pci_entries = sorted(os.listdir(self.pci_root))
            except OSError:
                pci_entries = []

            for dev in pci_entries:
                dev_dir = os.path.join(self.pci_root, dev)
                if not os.path.isdir(dev_dir):
                    continue
                vendor = _read_sysfs(os.path.join(dev_dir, "vendor"))
                if vendor != NVIDIA_VENDOR_ID:
                    continue
                dev_class = _read_sysfs(os.path.join(dev_dir, "class"))
                if not dev_class or not dev_class.lower().startswith(NVIDIA_DISPLAY_CLASS_PREFIX):
                    # Ignores audio functions (e.g. 0x040300)
                    continue

                self._nvidia_pci_id = dev
                self._nvidia_runtime_path = os.path.join(dev_dir, "power", "runtime_status")
                break

    def _read_cpu(self) -> float | None:
        if (self._cpu_path is None
                or not os.path.exists(self._cpu_path)
                or _read_sysfs(Path(self._cpu_path).parent / "name") != "coretemp"):
            self.rediscover()
        if self._cpu_path is None:
            return None
        val = _parse_millidegrees(self._cpu_path)
        if val is None and not os.path.exists(self._cpu_path):
            self.rediscover()
            if self._cpu_path and os.path.exists(self._cpu_path):
                val = _parse_millidegrees(self._cpu_path)
        return val

    def _read_acer_temps(self) -> dict[str, float | None]:
        if not self._acer_temp_paths:
            self.rediscover()
        elif any(
            not os.path.exists(p) or _read_sysfs(Path(p).parent / "name") != "acer"
            for p in self._acer_temp_paths.values()
        ):
            self.rediscover()

        result: dict[str, float | None] = {}
        for key, path in sorted(self._acer_temp_paths.items()):
            result[key] = _parse_millidegrees(path)
        return result

    def _read_gpu(self) -> float | None:
        if self._nvidia_runtime_path is None or not os.path.exists(self._nvidia_runtime_path):
            self.rediscover()
        if self._nvidia_runtime_path is None or self._nvidia_pci_id is None:
            return None

        dev_dir = Path(self.pci_root) / self._nvidia_pci_id
        if (_read_sysfs(dev_dir / "vendor") != NVIDIA_VENDOR_ID
                or not (_read_sysfs(dev_dir / "class") or "").lower().startswith(
                    NVIDIA_DISPLAY_CLASS_PREFIX
                )):
            self.rediscover()
            if self._nvidia_runtime_path is None or self._nvidia_pci_id is None:
                return None

        status = _read_sysfs(self._nvidia_runtime_path)
        if status is None and not os.path.exists(self._nvidia_runtime_path):
            self.rediscover()
            if self._nvidia_runtime_path:
                status = _read_sysfs(self._nvidia_runtime_path)

        # Only query nvidia-smi when the display function is active
        if status != "active":
            return None

        cmd = [
            "nvidia-smi",
            f"--id={self._nvidia_pci_id}",
            "--query-gpu=temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
        try:
            res = self.subprocess_runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.nvidia_smi_timeout,
            )
            if res.returncode == 0:
                raw = res.stdout.strip()
                if raw and raw.isdigit():
                    val = float(raw)
                    if math.isfinite(val) and 0.0 <= val <= 130.0:
                        return val
        except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError) as e:
            logger.debug("nvidia-smi query failed: %s", e)
            return None
        return None

    def __call__(self) -> dict[str, float | None]:
        """
        Poll temperatures. Returns dict with 'cpu', 'gpu', and neutral Acer temps.
        """
        temps: dict[str, float | None] = {
            "cpu": self._read_cpu(),
            "gpu": self._read_gpu(),
        }
        temps.update(self._read_acer_temps())
        return temps
