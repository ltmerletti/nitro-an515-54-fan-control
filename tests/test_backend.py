"""
Tests for nitrofan.backends -- Backend interface, MockBackend, and MockSensorProvider.
"""

import unittest
from unittest.mock import patch
from nitrofan.backends.base import Backend
from nitrofan.backends.mock import (
    MockBackend,
    MockSensorProvider,
    SimulatedSensorProvider,
)


class TestMockBackend(unittest.TestCase):
    """Test MockBackend operations and safety guarantees."""

    def setUp(self) -> None:
        self.backend = MockBackend(fans=["cpu", "gpu"])

    def test_initial_state(self) -> None:
        self.assertFalse(self.backend.is_open)
        self.assertTrue(self.backend.is_auto)
        self.assertEqual(self.backend.fans, ["cpu", "gpu"])
        self.assertIsNone(self.backend.read_percent("cpu"))
        self.assertIsNone(self.backend.read_percent("gpu"))

    def test_write_before_open_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self.backend.set_percent("cpu", 50)

    def test_open_and_set_percent(self) -> None:
        self.backend.open()
        self.assertTrue(self.backend.is_open)
        self.backend.set_percent("cpu", 50)
        self.assertFalse(self.backend.is_auto)
        self.assertEqual(self.backend.read_percent("cpu"), 50)
        self.assertIsNone(self.backend.read_percent("gpu"))

    def test_set_auto(self) -> None:
        self.backend.open()
        self.backend.set_percent("cpu", 60)
        self.backend.set_percent("gpu", 70)
        self.backend.set_auto()
        self.assertTrue(self.backend.is_auto)
        self.assertIsNone(self.backend.read_percent("cpu"))
        self.assertIsNone(self.backend.read_percent("gpu"))

    def test_rpm_calculation(self) -> None:
        self.backend.open()
        # In auto mode, returns synthetic auto RPM
        self.assertEqual(self.backend.read_rpm("cpu"), 2200)
        self.assertEqual(self.backend.read_rpm("gpu"), 2000)

        # In manual mode, RPM scales with percent
        self.backend.set_percent("cpu", 100)
        self.assertEqual(self.backend.read_rpm("cpu"), 5000)
        self.backend.set_percent("cpu", 0)
        self.assertEqual(self.backend.read_rpm("cpu"), 1800)

    def test_invalid_fan_raises(self) -> None:
        self.backend.open()
        with self.assertRaises(ValueError):
            self.backend.set_percent("fan3", 50)
        with self.assertRaises(ValueError):
            self.backend.read_percent("fan3")
        with self.assertRaises(ValueError):
            self.backend.read_rpm("fan3")

    def test_invalid_percent_raises(self) -> None:
        self.backend.open()
        for bad in (-1, 101, True, False, "50", 50.5):
            with self.assertRaises(ValueError):
                self.backend.set_percent("cpu", bad)  # type: ignore

    def test_close_is_idempotent_and_sets_auto(self) -> None:
        self.backend.open()
        self.backend.set_percent("cpu", 80)
        self.backend.close()
        self.assertFalse(self.backend.is_open)
        self.assertTrue(self.backend.is_auto)
        # Calling close again should not raise
        self.backend.close()
        self.assertFalse(self.backend.is_open)


class TestMockSensorProvider(unittest.TestCase):
    """Test MockSensorProvider thermal dynamics and interface."""

    def test_initial_temps(self) -> None:
        provider = MockSensorProvider({"cpu": 42.0, "gpu": 38.0})
        temps = provider()
        self.assertEqual(temps["cpu"], 42.0)
        self.assertEqual(temps["gpu"], 38.0)

    def test_set_temp(self) -> None:
        provider = MockSensorProvider()
        provider.set_temp("cpu", 88.5)
        provider.set_temp("gpu", None)
        self.assertEqual(provider()["cpu"], 88.5)
        self.assertIsNone(provider()["gpu"])

    def test_thermal_advance_cools_at_high_fan(self) -> None:
        provider = MockSensorProvider({"cpu": 70.0, "gpu": 70.0})
        # 100% fan cools temperature down
        provider.advance(5.0, {"cpu": 100, "gpu": 100})
        temps = provider()
        self.assertLess(temps["cpu"], 70.0)
        self.assertLess(temps["gpu"], 70.0)

    def test_thermal_advance_heats_at_low_fan(self) -> None:
        provider = MockSensorProvider({"cpu": 40.0, "gpu": 40.0})
        # 10% fan allows heat to rise
        provider.advance(5.0, {"cpu": 10, "gpu": 10})
        temps = provider()
        self.assertGreater(temps["cpu"], 40.0)
        self.assertGreater(temps["gpu"], 40.0)


class TestSimulatedSensorProvider(unittest.TestCase):
    """Test periodic sampling integration and backend fan telemetry coupling."""

    def test_backend_read_errors_reach_the_controller_safety_guard(self) -> None:
        backend = MockBackend()
        provider = SimulatedSensorProvider(MockSensorProvider(), backend, lambda: 10.0)
        with patch.object(backend, "read_percent", side_effect=OSError("EC unavailable")):
            with self.assertRaises(OSError):
                provider()

    def test_simulated_advance_with_clock_and_backend(self) -> None:
        now = 100.0
        def fake_clock() -> float:
            return now

        mock_backend = MockBackend()
        mock_backend.open()
        mock_backend.set_percent("cpu", 100)
        mock_backend.set_percent("gpu", 100)

        mock_sensor = MockSensorProvider({"cpu": 75.0, "gpu": 75.0})
        sim_provider = SimulatedSensorProvider(
            provider=mock_sensor,
            backend=mock_backend,
            clock=fake_clock,
        )

        # Advance fake clock by 5s
        now += 5.0
        temps = sim_provider()
        self.assertLess(temps["cpu"], 75.0)
        self.assertLess(temps["gpu"], 75.0)

        # Set low fan speed and advance clock
        mock_backend.set_percent("cpu", 10)
        mock_backend.set_percent("gpu", 10)
        t_after_cool = temps["cpu"]
        now += 10.0
        temps_hot = sim_provider()
        self.assertGreater(temps_hot["cpu"], t_after_cool)


if __name__ == "__main__":
    unittest.main()

