"""
mock.py - In-memory mock backend and sensor simulator for NitroFan.

Provides MockBackend for simulating fan control in testing and development
without hardware access, and MockSensorProvider for generating deterministic
temperature readings.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from nitrofan.backends.base import Backend

logger = logging.getLogger(__name__)


class MockBackend(Backend):
    """
    In-memory mock fan controller.

    Tracks fan speeds in memory and returns synthetic RPM values.
    Never interacts with sysfs, EC, or hardware.
    """

    def __init__(self, fans: list[str] | None = None, read_only: bool = False) -> None:
        self.name = "mock"
        self.read_only = read_only
        self.can_control = not read_only
        self.fans = list(fans) if fans is not None else ["cpu", "gpu"]
        self._is_open = False
        self._is_auto = True
        self._percents: dict[str, int | None] = {fan: None for fan in self.fans}

    @property
    def is_open(self) -> bool:
        """True if open() was called and backend has not been closed."""
        return self._is_open

    @property
    def is_auto(self) -> bool:
        """True if fans are currently under automatic control."""
        return self._is_auto

    def open(self) -> None:
        """Mark the backend as open and ready for commands."""
        self._is_open = True
        logger.debug("MockBackend: opened")

    def set_auto(self) -> None:
        """Reset fan speeds to mock automatic mode."""
        if self.read_only:
            return
        old = dict(self._percents)
        self._is_auto = True
        self._percents = {fan: None for fan in self.fans}
        logger.debug("MockBackend: set_auto (was %s)", old)

    def set_percent(self, fan: str, pct: int) -> None:
        """Set fan speed percent in memory."""
        if self.read_only:
            raise PermissionError("MockBackend is read-only; fan speed control is not supported")
        if not self._is_open:
            raise RuntimeError("Backend must be opened before writing fan speeds")
        if fan not in self.fans:
            raise ValueError(f"Unknown fan '{fan}'; available: {self.fans}")
        if not isinstance(pct, int) or isinstance(pct, bool) or not 0 <= pct <= 100:
            raise ValueError(f"Speed percent must be an integer in 0..100, got {pct}")

        old = self._percents.get(fan)
        self._is_auto = False
        self._percents[fan] = pct
        logger.debug("MockBackend: fan '%s' set_percent %s -> %s", fan, old, pct)

    def read_percent(self, fan: str) -> int | None:
        """Return the current set percent, or None if in auto."""
        if fan not in self.fans:
            raise ValueError(f"Unknown fan '{fan}'; available: {self.fans}")
        return self._percents.get(fan)

    def read_rpm(self, fan: str) -> int | None:
        """
        Return a synthetic tachometer reading.

        RPM scales linearly with set percent, or returns a baseline auto speed.
        """
        if fan not in self.fans:
            raise ValueError(f"Unknown fan '{fan}'; available: {self.fans}")
        pct = self._percents.get(fan)
        if pct is None:
            # Synthetic auto RPM
            return 2200 if fan == "cpu" else 2000
        base_rpm = 1800 if fan == "cpu" else 1700
        max_rpm = 5000 if fan == "cpu" else 4800
        return int(base_rpm + (pct / 100.0) * (max_rpm - base_rpm))

    def close(self) -> None:
        """Idempotently restore auto mode and close."""
        if self._is_open or not self._is_auto:
            try:
                self.set_auto()
            except Exception as e:
                logger.warning("MockBackend: set_auto failed during close: %s", e)
        self._is_open = False
        logger.debug("MockBackend: closed")


class MockSensorProvider:
    """
    Deterministic temperature sensor provider for testing.

    Simulates temperatures rising with lower fan speeds and falling with
    higher fan speeds when advance() is called. Can also be manually configured.
    """

    def __init__(self, initial_temps: Mapping[str, float | None] | None = None) -> None:
        if initial_temps is not None:
            self.temps: dict[str, float | None] = dict(initial_temps)
        else:
            self.temps = {"cpu": 45.0, "gpu": 40.0}

    def __call__(self) -> dict[str, float | None]:
        """Return current simulated temperatures."""
        return dict(self.temps)

    def set_temp(self, source: str, temp: float | None) -> None:
        """Directly set a temperature value for a sensor source."""
        self.temps[source] = temp

    def advance(self, dt: float, fan_percents: Mapping[str, int | None]) -> None:
        """
        Deterministically advance temperatures over a time delta dt.

        Higher fan percentages drive temperature toward cool target, lower
        percentages or auto allow heat load to raise temperature.
        """
        if dt <= 0:
            return
        base_ambient = 30.0
        load = 45.0
        for source in list(self.temps.keys()):
            curr = self.temps[source]
            if curr is None:
                continue
            fan_pct = fan_percents.get(source)
            effective_fan = 30.0 if fan_pct is None else float(fan_pct)
            cooling = (effective_fan / 100.0) * 45.0
            target = base_ambient + max(5.0, load - cooling)
            tau = 5.0
            alpha = min(1.0, max(0.0, dt / tau))
            new_t = curr + (target - curr) * alpha
            self.temps[source] = round(new_t, 2)


class SimulatedSensorProvider:
    """
    Periodic sensor provider wrapping MockSensorProvider.

    Advances temperatures based on elapsed time from an injected clock
    and fan speeds read from a backend, keeping Controller decoupled from
    the concrete simulation backend.
    """

    def __init__(
        self,
        provider: MockSensorProvider,
        backend: Backend,
        clock: Callable[[], float],
    ) -> None:
        self.provider = provider
        self.backend = backend
        self.clock = clock
        self.last_advance_time = clock()

    def __call__(self) -> dict[str, float | None]:
        now = self.clock()
        dt = max(0.0, now - self.last_advance_time)
        self.last_advance_time = now
        fan_percents: dict[str, int | None] = {}
        for fan in self.backend.fans:
            fan_percents[fan] = self.backend.read_percent(fan)
        self.provider.advance(dt, fan_percents)
        return self.provider()

