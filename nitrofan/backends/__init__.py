"""
backends package - Fan control backend interfaces and implementations.
"""

from nitrofan.backends.base import Backend
from nitrofan.backends.ec import ECBackend
from nitrofan.backends.hwmon import HwmonBackend
from nitrofan.backends.mock import MockBackend, MockSensorProvider
from nitrofan.backends.nbfc import NBFCBackend

__all__ = ["Backend", "ECBackend", "HwmonBackend", "MockBackend", "MockSensorProvider", "NBFCBackend"]
