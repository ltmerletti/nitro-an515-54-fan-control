"""
base.py - Base interface for NitroFan hardware and mock backends.

Defines the abstract Backend class that any fan controller backend must
implement. Hardware-specific backends and testing mocks both conform to
this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping


class Backend(ABC):
    """
    Abstract interface for fan control backends.

    Subclasses must provide a list of fan identifiers, methods to open and
    close the backend, switch between automatic and manual percent modes,
    and read back speed percentages and RPM values.
    """

    fans: list[str]
    name: str = "base"
    read_only: bool = False
    can_control: bool = True

    @abstractmethod
    def open(self) -> None:
        """
        Open connections, acquire file descriptors, or initialize state.
        Must be called before setting or reading fan speeds.
        """

    @abstractmethod
    def set_auto(self) -> None:
        """
        Return all fans to automatic hardware or firmware control.
        """

    @abstractmethod
    def set_percent(self, fan: str, pct: int) -> None:
        """
        Set target fan speed as an integer percentage (0..100).
        """

    def set_targets(self, targets: Mapping[str, int]) -> None:
        """
        Batch set target speeds across multiple fans.

        Default implementation calls set_percent sequentially for each fan.
        Backends that support batch or atomic updates may override this.
        """
        for fan, pct in targets.items():
            self.set_percent(fan, pct)

    @abstractmethod
    def read_percent(self, fan: str) -> int | None:
        """
        Read back the current fan speed percentage, or None if in auto mode.
        """

    @abstractmethod
    def read_rpm(self, fan: str) -> int | None:
        """
        Read the current tachometer RPM for the specified fan.
        """

    @abstractmethod
    def close(self) -> None:
        """
        Clean up resources and restore automatic control.
        Must be idempotent and safe to call multiple times.
        """
