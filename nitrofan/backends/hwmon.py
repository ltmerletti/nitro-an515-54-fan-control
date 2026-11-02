"""
hwmon.py - Read-only hwmon backend for Acer Nitro AN515-54 fan telemetry.

Discovers the 'acer' hwmon device exposed by acer_wmi (predator_v4=1) and reads
fan tachometer RPM from fan1_input and fan2_input.

Hardware control Phase 0 is incomplete. This backend is strictly read-only:
it performs zero sysfs or hardware writes in open, set_auto, set_percent, or close.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from nitrofan.backends.base import Backend

logger = logging.getLogger(__name__)


def _read_sysfs(path: str | Path) -> str | None:
    """Read stripped text from sysfs, or None on error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


class HwmonBackend(Backend):
    """
    Read-only fan backend for Linux sysfs hwmon telemetry.

    Discovers 'acer' hwmon device by name, reads tachometer RPM from fan1/fan2,
    and explicitly rejects all write operations.
    """

    def __init__(
        self,
        sysfs_root: str | Path = "",
        hwmon_root: str | Path | None = None,
    ) -> None:
        self.name = "hwmon"
        self.read_only = True
        self.can_control = False
        self.sysfs_root = str(sysfs_root)
        if hwmon_root is not None:
            self.hwmon_root = str(hwmon_root)
        elif self.sysfs_root:
            self.hwmon_root = os.path.join(self.sysfs_root, "sys", "class", "hwmon")
        else:
            self.hwmon_root = "/sys/class/hwmon"

        self.fans: list[str] = ["fan1", "fan2"]
        self._is_open = False
        self._acer_hwmon_dir: str | None = None
        self._fan_paths: dict[str, str] = {}

    @property
    def is_open(self) -> bool:
        """True if open() was called and backend has not been closed."""
        return self._is_open

    def _discover(self) -> None:
        """
        Scan hwmon root for the 'acer' device and its fan inputs.

        Raises FileNotFoundError or RuntimeError if the device is not found.
        """
        if not os.path.isdir(self.hwmon_root):
            raise FileNotFoundError(f"hwmon directory '{self.hwmon_root}' not found")

        try:
            entries = sorted(os.listdir(self.hwmon_root))
        except OSError as e:
            raise RuntimeError(f"Cannot read hwmon directory '{self.hwmon_root}': {e}") from e

        acer_dir: str | None = None
        for entry in entries:
            hwmon_path = os.path.join(self.hwmon_root, entry)
            if not os.path.isdir(hwmon_path):
                continue
            name = _read_sysfs(os.path.join(hwmon_path, "name"))
            if name == "acer":
                acer_dir = hwmon_path
                break

        if acer_dir is None:
            raise RuntimeError(
                "acer hwmon device not found; ensure 'acer_wmi' module is loaded with 'predator_v4=1'"
            )

        self._acer_hwmon_dir = acer_dir
        fan_pattern = re.compile(r"^fan(\d+)_input$")
        discovered_paths: dict[str, str] = {}

        try:
            dir_files = sorted(os.listdir(acer_dir))
        except OSError as e:
            raise RuntimeError(f"Cannot read acer hwmon directory '{acer_dir}': {e}") from e

        for f in dir_files:
            m = fan_pattern.match(f)
            if m:
                fan_id = f"fan{m.group(1)}"
                discovered_paths[fan_id] = os.path.join(acer_dir, f)

        if not discovered_paths:
            raise RuntimeError(f"No fan inputs found in acer hwmon device at '{acer_dir}'")

        self._fan_paths = discovered_paths
        self.fans = sorted(discovered_paths.keys())
        logger.debug("HwmonBackend discovered fans %s in %s", self.fans, acer_dir)

    def open(self) -> None:
        """
        Discover acer hwmon device and initialize fan paths.
        Safe to call multiple times.
        """
        self._discover()
        self._is_open = True
        logger.debug("HwmonBackend: opened (fans: %s)", self.fans)

    def set_auto(self) -> None:
        """
        Read-only backend: firmware remains in automatic control.
        Zero hardware writes.
        """
        # Monitoring only: firmware manages fan speeds natively.

    def set_percent(self, fan: str, pct: int) -> None:
        """
        Explicitly reject fan speed writes.

        Phase 0 hardware control is unverified; writing to hwmon or EC is forbidden.
        """
        raise PermissionError(
            f"HwmonBackend is read-only; cannot set fan '{fan}' to {pct}%. "
            "Hardware fan control is not supported."
        )

    def read_percent(self, fan: str) -> int | None:
        """
        Read fan speed percentage.

        Always returns None because RPM is not percent, and hwmon exposes
        no PWM duty cycle percentage.
        """
        if fan not in self.fans:
            raise ValueError(f"Unknown fan '{fan}'; available: {self.fans}")
        return None

    def read_rpm(self, fan: str) -> int | None:
        """
        Read fan tachometer RPM. Returns nonnegative integer or None.
        """
        if fan not in self.fans:
            raise ValueError(f"Unknown fan '{fan}'; available: {self.fans}")
        if not self._is_open:
            raise RuntimeError("Backend must be opened before reading fan speeds")

        path = self._fan_paths.get(fan)
        if (not path or not os.path.exists(path)
                or _read_sysfs(Path(path).parent / "name") != "acer"):
            try:
                self._discover()
                path = self._fan_paths.get(fan)
            except Exception:
                return None

        if not path:
            return None

        raw = _read_sysfs(path)
        if raw is None:
            return None
        try:
            val = int(raw)
            return val if val >= 0 else None
        except ValueError:
            return None

    def close(self) -> None:
        """
        Idempotently close the backend. Zero hardware writes.
        """
        self._is_open = False
        logger.debug("HwmonBackend: closed")
