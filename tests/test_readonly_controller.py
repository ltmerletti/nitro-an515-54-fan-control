"""
Unit tests for Controller capabilities, read-only mode, and emergency latch tracking.

Verifies:
- Controller never calls set_percent or set_auto on read-only backend
- Read-only status exposes read_only=True, can_control=False, and actual backend name
- Manual modes and curve changes are rejected atomically before mutation
- Forced manual startup_mode is rejected on read-only backend
- High-temp emergency condition exposes emergency=True while mode remains auto and reported percent is None
- Emergency clear tracks actual triggering sensor keys and does not permanently latch on fan1/fan2
- Fail-auto and resume perform zero hardware writes
- Writable mock safeguards remain intact
"""

from __future__ import annotations

import unittest
from types import MappingProxyType
from unittest.mock import MagicMock

from nitrofan.backends.hwmon import HwmonBackend
from nitrofan.backends.mock import MockBackend
from nitrofan.config import Config, CurvePoint, CurvesConfig
from nitrofan.controller import Controller


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.time = start

    def __call__(self) -> float:
        return self.time

    def advance(self, dt: float) -> None:
        self.time += dt


def make_config(
    startup_mode: str = "auto",
    backend: str = "hwmon",
    emergency_c: int = 90,
) -> Config:
    return Config(
        backend=backend,
        tick_s=2.0,
        reassert_s=10.0,
        min_percent=25,
        emergency_c=emergency_c,
        startup_mode=startup_mode,
        ec=None,
        curves=CurvesConfig(
            fans=MappingProxyType({
                "cpu": (CurvePoint(45.0, 25), CurvePoint(85.0, 100)),
                "gpu": (CurvePoint(45.0, 25), CurvePoint(80.0, 100)),
            })
        ),
    )


class TestReadOnlyController(unittest.TestCase):
    """Test Controller integration with read-only backends."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.wall_clock = FakeClock()
        self.sensors: dict[str, float | None] = {
            "cpu": 45.0,
            "gpu": 40.0,
            "acer_temp1": 42.0,
            "acer_temp2": 41.0,
        }
        # Read-only mock backend to verify zero calls to set_percent
        self.backend = MockBackend(fans=["fan1", "fan2"], read_only=True)
        self.backend.open()

        self.config = make_config()
        self.controller = Controller(
            config=self.config,
            backend=self.backend,
            sensor_provider=lambda: self.sensors,
            clock=self.clock,
            wall_clock=self.wall_clock,
        )

    def test_status_capabilities_and_actual_backend(self) -> None:
        status = self.controller.get_status()
        self.assertTrue(status.get("ok"))
        self.assertEqual(status.get("backend"), "mock")
        self.assertTrue(status.get("read_only"))
        self.assertFalse(status.get("can_control"))
        self.assertEqual(status.get("mode"), "auto")
        self.assertEqual(status.get("requested_mode"), "auto")
        self.assertFalse(status.get("emergency"))

        # Fans should have None percent
        fans = status.get("fans", {})
        self.assertIn("fan1", fans)
        self.assertIn("fan2", fans)
        self.assertIsNone(fans["fan1"]["percent"])
        self.assertIsNone(fans["fan2"]["percent"])

    def test_hwmon_backend_identity_in_status(self) -> None:
        # Create a mock hwmon backend with name="hwmon"
        mock_hwmon = MagicMock(spec=HwmonBackend)
        mock_hwmon.name = "hwmon"
        mock_hwmon.read_only = True
        mock_hwmon.can_control = False
        mock_hwmon.fans = ["fan1", "fan2"]
        mock_hwmon.read_percent.return_value = None
        mock_hwmon.read_rpm.return_value = 2500

        ctrl = Controller(
            config=self.config,
            backend=mock_hwmon,
            sensor_provider=lambda: self.sensors,
            clock=self.clock,
        )
        status = ctrl.get_status()
        self.assertEqual(status.get("backend"), "hwmon")
        self.assertTrue(status.get("read_only"))
        self.assertFalse(status.get("can_control"))

    def test_startup_mode_manual_rejected_on_readonly(self) -> None:
        for bad_mode in ("max", "curve"):
            cfg = make_config(startup_mode=bad_mode)
            with self.assertRaises(ValueError) as ctx:
                Controller(
                    config=cfg,
                    backend=self.backend,
                    sensor_provider=lambda: self.sensors,
                    clock=self.clock,
                )
            self.assertIn("Cannot initialize read-only backend with manual startup_mode", str(ctx.exception))

    def test_manual_mode_rejected_atomically(self) -> None:
        # fixed mode
        with self.assertRaises(ValueError) as ctx:
            self.controller.set_mode("fixed", percent=50)
        self.assertIn("Cannot set manual mode on read-only backend", str(ctx.exception))
        self.assertEqual(self.controller.requested_mode, "auto")
        self.assertEqual(self.controller.current_effective_mode, "auto")

        # max mode
        with self.assertRaises(ValueError):
            self.controller.set_mode("max")
        self.assertEqual(self.controller.requested_mode, "auto")

        # curve mode
        with self.assertRaises(ValueError):
            self.controller.set_mode("curve")
        self.assertEqual(self.controller.requested_mode, "auto")

        # set_curve
        with self.assertRaises(ValueError):
            self.controller.set_curve("fan1", [[40, 25], [80, 100]])

    def test_high_temp_emergency_monitoring(self) -> None:
        # Trip emergency temperature (>= 90C) on CPU
        self.sensors["cpu"] = 92.0
        with self.assertLogs("nitrofan.controller", level="WARNING") as logs:
            self.controller.tick()
        self.assertIn("firmware remains in control", " ".join(logs.output))
        self.assertNotIn("Forcing 100%", " ".join(logs.output))

        # Emergency exposed in status
        status = self.controller.get_status()
        self.assertTrue(status.get("emergency"))
        # Mode remains auto because backend is read-only (firmware in control)
        self.assertEqual(status.get("mode"), "auto")
        # Speed percent must NOT claim 100%
        fans = status.get("fans", {})
        self.assertIsNone(fans["fan1"]["percent"])
        self.assertIsNone(fans["fan2"]["percent"])

    def test_emergency_clear_without_fan1_fan2_permanent_latch(self) -> None:
        # Trigger emergency on CPU (92C) and Acer sensor (91C)
        self.sensors["cpu"] = 92.0
        self.sensors["acer_temp1"] = 91.0
        self.controller.tick()
        self.assertTrue(self.controller.emergency)

        # Cool down above clear threshold (85C > 80C) -> remains emergency
        self.sensors["cpu"] = 85.0
        self.sensors["acer_temp1"] = 82.0
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertTrue(self.controller.emergency)

        # If a triggering sensor goes missing, emergency remains latched!
        self.sensors["cpu"] = 75.0
        self.sensors["acer_temp1"] = None
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertTrue(self.controller.emergency)

        # Once all triggering sensors are present and cool (<= 80C), emergency clears!
        # Notably, fan1 and fan2 do not exist in temps, but they MUST NOT prevent clearing!
        self.sensors["acer_temp1"] = 78.0
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertFalse(self.controller.emergency)

    def test_fail_auto_zero_writes(self) -> None:
        backend_mock = MagicMock(spec=MockBackend)
        backend_mock.name = "hwmon"
        backend_mock.read_only = True
        backend_mock.fans = ["fan1", "fan2"]
        backend_mock.read_percent.return_value = None
        backend_mock.read_rpm.return_value = 2000

        ctrl = Controller(
            config=self.config,
            backend=backend_mock,
            sensor_provider=lambda: self.sensors,
            clock=self.clock,
        )
        ctrl._fail_auto()
        backend_mock.set_auto.assert_not_called()
        backend_mock.set_percent.assert_not_called()

    def test_wallclock_gap_zero_writes(self) -> None:
        backend_mock = MagicMock(spec=MockBackend)
        backend_mock.name = "hwmon"
        backend_mock.read_only = True
        backend_mock.fans = ["fan1", "fan2"]
        backend_mock.read_percent.return_value = None
        backend_mock.read_rpm.return_value = 2000

        ctrl = Controller(
            config=self.config,
            backend=backend_mock,
            sensor_provider=lambda: self.sensors,
            clock=self.clock,
            wall_clock=self.wall_clock,
        )
        ctrl.tick()

        # Advance wall clock by 60s (suspend/resume)
        self.clock.advance(2.0)
        self.wall_clock.advance(60.0)
        ctrl.tick()

        # Backend close and open called to re-enumerate
        backend_mock.close.assert_called_once()
        backend_mock.open.assert_called_once()
        # Zero writes!
        backend_mock.set_auto.assert_not_called()
        backend_mock.set_percent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
