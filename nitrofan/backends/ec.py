"""
ec.py - Read-only Embedded Controller (EC) backend for Acer Nitro AN515-54 fan telemetry.

Reads tachometer fan RPM by decoding configured 16-bit little-endian EC register pairs
via ECReader.

Phase 0 hardware control is incomplete. This backend is strictly read-only:
it performs zero hardware or EC writes in open, set_auto, set_percent, or close.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from nitrofan.backends.base import Backend
from nitrofan.config import ECRpmFan
from nitrofan.ec import ECReader

logger = logging.getLogger(__name__)


class ECBackend(Backend):
    """
    Read-only fan backend for ACPI Embedded Controller RPM telemetry.

    Decodes fan tachometer RPM from caller-configured 16-bit register pairs
    using ECReader, and explicitly rejects all hardware control write operations.
    """

    def __init__(
        self,
        ec_rpm: Mapping[str, ECRpmFan],
        ec_path: str | Path | None = None,
        reader: ECReader | None = None,
    ) -> None:
        self.name = "ec"
        self.read_only = True
        self.can_control = False

        if not ec_rpm:
            raise ValueError(
                "ECBackend requires a non-empty ec_rpm mapping. "
                "Hardware control and fallback to candidate maps are not supported."
            )

        self._ec_rpm: dict[str, ECRpmFan] = dict(ec_rpm)
        self.fans: list[str] = sorted(self._ec_rpm.keys())
        self._reader: ECReader = reader if reader is not None else ECReader(ec_path=ec_path)
        self._is_open = False

    @property
    def is_open(self) -> bool:
        """True if open() was called and backend has not been closed."""
        return self._is_open

    def open(self) -> None:
        """
        Open the ECReader interface. Safe to call multiple times.
        Supports reopening after a close (e.g. on suspend/resume).
        """
        self._reader.open()
        self._is_open = True
        logger.debug("ECBackend opened with fans %s via '%s'", self.fans, self._reader.path)

    def close(self) -> None:
        """
        Close the ECReader interface. Idempotent. Zero hardware writes.
        """
        self._is_open = False
        self._reader.close()
        logger.debug("ECBackend closed")

    def set_auto(self) -> None:
        """
        Read-only backend: firmware remains in automatic control.
        Zero hardware writes.
        """
        # Monitoring only: firmware manages fan speeds natively.

    def set_percent(self, fan: str, pct: int) -> None:
        """
        Explicitly reject fan speed writes.

        Phase 0 hardware control is unverified; writing to EC is forbidden.
        """
        raise PermissionError(
            f"ECBackend is read-only; cannot set fan '{fan}' to {pct}%. "
            "Hardware fan control is not supported."
        )

    def read_percent(self, fan: str) -> int | None:
        """
        Read fan speed percentage.

        Always returns None because RPM readings are tachometer frequencies,
        not duty cycle percentages.
        """
        if fan not in self.fans:
            raise ValueError(f"Unknown fan '{fan}'; available: {self.fans}")
        return None

    def read_rpm(self, fan: str) -> int | None:
        """
        Read fan tachometer RPM for the configured fan.

        Decodes the configured low and high register bytes using ECReader.
        Returns nonnegative integer RPM on success, or None on read failure
        with a diagnostic log message. Never infers or returns percentage.
        """
        if fan not in self.fans:
            raise ValueError(f"Unknown fan '{fan}'; available: {self.fans}")
        if not self._is_open:
            raise RuntimeError("Backend must be opened before reading fan speeds")

        fan_cfg = self._ec_rpm[fan]
        if fan_cfg.encoding != "u16_le":
            logger.warning("Unsupported encoding '%s' for fan '%s'", fan_cfg.encoding, fan)
            return None

        try:
            val = self._reader.read_u16_le(fan_cfg.low_register, fan_cfg.high_register)
            return val if val >= 0 else None
        except (OSError, RuntimeError) as err:
            logger.warning("Failed reading EC RPM for fan '%s': %s", fan, err)
            return None
