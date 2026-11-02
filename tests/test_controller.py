"""
Tests for nitrofan.controller -- Deterministic safety rules and speed control.
"""

from collections.abc import Mapping
from types import MappingProxyType
import json
import unittest
from unittest.mock import patch

from nitrofan.backends.base import Backend
from nitrofan.config import Config, ConfigError, CurvePoint, CurvesConfig
from nitrofan.controller import Controller


class FakeClock:
    """Deterministic simulated clock."""

    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class SpyBackend(Backend):
    """Spy backend tracking writes, open/close calls, and simulating errors."""

    def __init__(self, fans: list[str] | None = None) -> None:
        self.fans = list(fans) if fans is not None else ["cpu", "gpu"]
        self.writes: list[tuple[str, ...]] = []
        self.current_percents: dict[str, int | None] = {f: None for f in self.fans}
        self.is_auto = True
        self.raise_on_set_percent = False
        self.raise_on_set_auto = False
        self.raise_on_read_percent = False
        self.open_count = 0
        self.close_count = 0

    def open(self) -> None:
        self.open_count += 1

    def set_auto(self) -> None:
        if self.raise_on_set_auto:
            raise RuntimeError("simulated set_auto failure")
        self.writes.append(("set_auto",))
        self.is_auto = True
        self.current_percents = {f: None for f in self.fans}

    def set_percent(self, fan: str, pct: int) -> None:
        if self.raise_on_set_percent:
            raise RuntimeError(f"simulated set_percent failure on {fan}")
        self.writes.append(("set_percent", fan, pct))
        self.is_auto = False
        self.current_percents[fan] = pct

    def read_percent(self, fan: str) -> int | None:
        if self.raise_on_read_percent:
            raise RuntimeError("simulated read_percent failure")
        return self.current_percents.get(fan)

    def read_rpm(self, fan: str) -> int | None:
        pct = self.current_percents.get(fan)
        return 2000 if pct is None else int(1500 + pct * 30)

    def close(self) -> None:
        self.close_count += 1
        self.set_auto()


def make_test_config(
    startup_mode: str = "auto",
    min_percent: int = 25,
    emergency_c: int = 90,
    tick_s: float = 2.0,
    reassert_s: float = 10.0,
) -> Config:
    return Config(
        backend="mock",
        tick_s=tick_s,
        reassert_s=reassert_s,
        min_percent=min_percent,
        emergency_c=emergency_c,
        startup_mode=startup_mode,
        ec=None,
        curves=CurvesConfig(
            fans=MappingProxyType({
                "cpu": (
                    CurvePoint(45.0, 25),
                    CurvePoint(60.0, 40),
                    CurvePoint(70.0, 60),
                    CurvePoint(80.0, 85),
                    CurvePoint(88.0, 100),
                ),
                "gpu": (
                    CurvePoint(45.0, 25),
                    CurvePoint(60.0, 40),
                    CurvePoint(70.0, 60),
                    CurvePoint(78.0, 85),
                    CurvePoint(85.0, 100),
                ),
            })
        ),
    )


class TestControllerModesAndSafety(unittest.TestCase):
    """Test deterministic controller mode dispatch and safety enforcement."""

    def setUp(self) -> None:
        self.clock = FakeClock(100.0)
        self.wall_clock = FakeClock(1000.0)
        self.backend = SpyBackend(fans=["cpu", "gpu"])
        self.sensors: dict[str, float | None] = {"cpu": 50.0, "gpu": 45.0}
        self.config = make_test_config()
        self.controller = Controller(
            config=self.config,
            backend=self.backend,
            sensor_provider=lambda: self.sensors,
            clock=self.clock,
            wall_clock=self.wall_clock,
        )

    def test_startup_default_auto(self) -> None:
        self.assertEqual(self.controller.requested_mode, "auto")
        self.controller.tick()
        self.assertIn(("set_auto",), self.backend.writes)

    def test_mode_max(self) -> None:
        self.controller.set_mode("max")
        self.backend.writes.clear()
        self.controller.tick()
        self.assertEqual(
            self.backend.writes,
            [("set_percent", "cpu", 100), ("set_percent", "gpu", 100)],
        )

    def test_mode_fixed_scalar(self) -> None:
        self.controller.set_mode("fixed", percent=50)
        self.backend.writes.clear()
        self.controller.tick()
        self.assertEqual(
            self.backend.writes,
            [("set_percent", "cpu", 50), ("set_percent", "gpu", 50)],
        )

    def test_mode_fixed_per_fan_map(self) -> None:
        self.controller.set_mode("fixed", percents={"cpu": 40, "gpu": 65})
        self.backend.writes.clear()
        self.controller.tick()
        self.assertEqual(
            self.backend.writes,
            [("set_percent", "cpu", 40), ("set_percent", "gpu", 65)],
        )

    def test_mode_fixed_canonical_percent_mapping(self) -> None:
        # Finding 8: Canonical protocol fixed per-fan request is percent: {"cpu": 60, "gpu": 40}
        self.controller.set_mode("fixed", percent={"cpu": 60, "gpu": 40})
        self.backend.writes.clear()
        self.controller.tick()
        self.assertEqual(
            self.backend.writes,
            [("set_percent", "cpu", 60), ("set_percent", "gpu", 40)],
        )

    def test_mode_fixed_contradictory_payloads_rejected(self) -> None:
        # Finding 8: contradictory payloads rejected atomically
        with self.assertRaises(ValueError):
            self.controller.set_mode("fixed", percent=50, percents={"cpu": 60, "gpu": 40})

    def test_mode_fixed_rejects_zero_and_below_floor(self) -> None:
        # Zero rejected
        with self.assertRaises(ValueError):
            self.controller.set_mode("fixed", percent=0)
        # Below floor rejected
        with self.assertRaises(ValueError):
            self.controller.set_mode("fixed", percent=20)
        # Invalid in mapping rejected without modifying state
        with self.assertRaises(ValueError):
            self.controller.set_mode("fixed", percents={"cpu": 50, "gpu": 10})
        # Missing fan rejected
        with self.assertRaises(ValueError):
            self.controller.set_mode("fixed", percents={"cpu": 50})
        # Unknown fan rejected
        with self.assertRaises(ValueError):
            self.controller.set_mode("fixed", percents={"cpu": 50, "gpu": 50, "fan3": 50})

    def test_mode_curve_interpolation(self) -> None:
        self.controller.set_mode("curve")
        # CPU at 60C -> curve specifies 40%
        # GPU at 45C -> curve specifies 25%
        self.sensors = {"cpu": 60.0, "gpu": 45.0}
        self.backend.writes.clear()
        self.controller.tick()
        self.assertEqual(
            self.backend.writes,
            [("set_percent", "cpu", 40), ("set_percent", "gpu", 25)],
        )

    def test_fixed_to_curve_preserves_speed_then_cools_once_per_tick(self) -> None:
        self.controller.tick()
        self.controller.set_mode("fixed", percent=60)
        self.assertTrue(self.controller.apply_command())
        self.controller.set_mode("curve")
        for _ in range(5):
            self.controller.set_mode("curve")
            self.assertTrue(self.controller.apply_command())
        self.assertEqual(self.backend.current_percents, {"cpu": 60, "gpu": 60})
        self.sensors["cpu"] += 1
        self.sensors["gpu"] += 1
        self.clock.advance(2)
        self.controller.tick()
        self.assertEqual(self.controller.current_effective_mode, "curve")
        self.assertEqual(self.backend.current_percents, {"cpu": 50, "gpu": 50})
        self.assertIsNone(self.controller.last_error)

    def test_command_reports_backend_failure_and_returns_to_auto(self) -> None:
        self.controller.tick()
        self.backend.raise_on_set_percent = True
        self.controller.set_mode("fixed", percent=60)
        self.assertFalse(self.controller.apply_command())
        self.assertEqual(self.controller.current_effective_mode, "auto")
        self.assertTrue(self.backend.is_auto)

    def test_sensor_exception_returns_to_auto(self) -> None:
        self.controller.set_mode("fixed", percent=60)
        self.controller.tick()
        def fail_sensor():
            raise OSError("sensor unavailable")
        self.controller.sensor_provider = fail_sensor
        with self.assertLogs("nitrofan.controller", level="ERROR"):
            self.controller.tick()
        self.assertTrue(self.backend.is_auto)
        self.assertEqual(self.controller.requested_mode, "auto")
        self.assertIn("Sensor error", self.controller.last_error)

    def test_oversized_curve_update_keeps_previous_curve(self) -> None:
        previous = self.controller.curves["cpu"]
        with self.assertRaises(ValueError):
            self.controller.set_curve("cpu", [[temperature, 50] for temperature in range(4000)])
        self.assertEqual(self.controller.curves["cpu"], previous)

    def test_partial_telemetry_failure_discards_previous_manual_values(self) -> None:
        self.controller.set_mode("fixed", percent=60)
        self.controller.tick()
        read_rpm = self.backend.read_rpm
        def fail_gpu(fan):
            if fan == "gpu":
                raise OSError("tachometer unavailable")
            return read_rpm(fan)
        with patch.object(self.backend, "read_rpm", side_effect=fail_gpu):
            status = self.controller.get_status()
        self.assertEqual(status["mode"], "auto")
        self.assertTrue(all(data["percent"] is None for data in status["fans"].values()))

    def test_curve_floor_applied(self) -> None:
        self.controller.set_mode("curve")
        # Low temp 30C would yield < min_percent without floor; must clamp to min_percent (25%)
        self.sensors = {"cpu": 30.0, "gpu": 30.0}
        self.backend.writes.clear()
        self.controller.tick()
        self.assertEqual(
            self.backend.writes,
            [("set_percent", "cpu", 25), ("set_percent", "gpu", 25)],
        )

    def test_independent_fan_curves(self) -> None:
        self.controller.set_mode("curve")
        # CPU 70C -> 60%
        # GPU 78C -> 85%
        self.sensors = {"cpu": 70.0, "gpu": 78.0}
        self.backend.writes.clear()
        self.controller.tick()
        self.assertEqual(
            self.backend.writes,
            [("set_percent", "cpu", 60), ("set_percent", "gpu", 85)],
        )

    def test_change_only_and_reassert_writes(self) -> None:
        self.controller.set_mode("fixed", percent=50)
        self.backend.writes.clear()

        # Tick 1: writes target 50%
        self.controller.tick()
        self.assertEqual(len(self.backend.writes), 2)
        self.backend.writes.clear()

        # Tick 2 (1 sec later, target unchanged, reassert_s=10 not reached): NO write
        self.clock.advance(1.0)
        self.wall_clock.advance(1.0)
        self.controller.tick()
        self.assertEqual(len(self.backend.writes), 0)

        # Tick 3 (10 sec later, reassert_s elapsed): reasserts writes
        self.clock.advance(10.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertEqual(len(self.backend.writes), 2)

    def test_reassert_auto(self) -> None:
        self.controller.set_mode("auto")
        self.backend.writes.clear()
        self.controller.tick()
        self.assertEqual(self.backend.writes, [("set_auto",)])
        self.backend.writes.clear()

        # Next tick before reassert: no write
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertEqual(len(self.backend.writes), 0)

        # After reassert_s: reasserts set_auto
        self.clock.advance(10.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertEqual(self.backend.writes, [("set_auto",)])


class TestControllerSensorLossAndEmergency(unittest.TestCase):
    """Test sensor fault latching, emergency overrides, and hysteresis."""

    def setUp(self) -> None:
        self.clock = FakeClock(100.0)
        self.wall_clock = FakeClock(1000.0)
        self.backend = SpyBackend(fans=["cpu", "gpu"])
        self.sensors: dict[str, float | None] = {"cpu": 50.0, "gpu": 45.0}
        self.config = make_test_config(startup_mode="curve")
        self.controller = Controller(
            config=self.config,
            backend=self.backend,
            sensor_provider=lambda: self.sensors,
            clock=self.clock,
            wall_clock=self.wall_clock,
        )

    def test_cpu_sensor_loss_3_consecutive_ticks(self) -> None:
        # Tick 1: CPU sensor None
        self.sensors["cpu"] = None
        self.controller.tick()
        self.assertEqual(self.controller.cpu_missing_count, 1)
        self.assertFalse(self.controller.cpu_fault)
        self.assertEqual(self.controller.current_effective_mode, "curve")

        # Tick 2: CPU sensor None
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertEqual(self.controller.cpu_missing_count, 2)
        self.assertFalse(self.controller.cpu_fault)

        # Tick 3: 3rd consecutive None triggers fault, switches requested_mode to auto!
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertEqual(self.controller.cpu_missing_count, 3)
        self.assertTrue(self.controller.cpu_fault)
        self.assertEqual(self.controller.requested_mode, "auto")
        self.assertEqual(self.controller.current_effective_mode, "auto")
        self.assertIsNotNone(self.controller.last_error)

        # Healthy reading on tick 4 clears fault, but mode stays auto per spec (no implicit return to manual)
        self.sensors["cpu"] = 55.0
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertEqual(self.controller.cpu_missing_count, 0)
        self.assertFalse(self.controller.cpu_fault)
        self.assertEqual(self.controller.requested_mode, "auto")
        self.assertEqual(self.controller.current_effective_mode, "auto")

    def test_mode_change_cannot_bypass_cpu_fault(self) -> None:
        # Trip CPU fault
        self.sensors["cpu"] = None
        for _ in range(3):
            self.clock.advance(2.0)
            self.wall_clock.advance(2.0)
            self.controller.tick()
        self.assertTrue(self.controller.cpu_fault)
        self.assertEqual(self.controller.requested_mode, "auto")

        # Client requests "fixed" mode while sensor fault active: must be rejected clearly
        with self.assertRaises(ValueError):
            self.controller.set_mode("fixed", percent=60)
        # Counter and requested mode must NOT be modified
        self.assertEqual(self.controller.requested_mode, "auto")
        self.assertEqual(self.controller.cpu_missing_count, 3)

        # Next tick still missing CPU temp: stays in auto
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertEqual(self.controller.current_effective_mode, "auto")
        self.assertTrue(self.controller.cpu_fault)

    def test_gpu_sensor_missing_curve_behavior(self) -> None:
        # GPU is None (e.g. asleep). Documented behavior: min_percent floor, no invented 0C
        self.sensors = {"cpu": 60.0, "gpu": None}
        self.backend.writes.clear()
        self.controller.tick()
        # CPU uses curve (40%), GPU safely stays at floor (25%)
        self.assertEqual(
            self.backend.writes,
            [("set_percent", "cpu", 40), ("set_percent", "gpu", 25)],
        )

    def test_emergency_override_and_hysteresis(self) -> None:
        # Normal curve mode
        self.sensors = {"cpu": 50.0, "gpu": 50.0}
        self.controller.tick()
        self.assertFalse(self.controller.emergency)

        # Emergency triggered (>= 90C)
        self.sensors["cpu"] = 91.0
        self.backend.writes.clear()
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()

        self.assertTrue(self.controller.emergency)
        self.assertEqual(self.controller.current_effective_mode, "emergency")
        # Forces both fans to 100% immediately
        self.assertEqual(
            self.backend.writes,
            [("set_percent", "cpu", 100), ("set_percent", "gpu", 100)],
        )

        # Client tries to set mode to fixed 30% while in emergency: cannot bypass override!
        self.controller.set_mode("fixed", percent=30)
        self.assertEqual(self.controller.requested_mode, "fixed")
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.backend.writes.clear()
        self.controller.tick()
        self.assertTrue(self.controller.emergency)
        self.assertEqual(self.controller.current_effective_mode, "emergency")

        # Temp drops to 85C (below 90C, but not 10C below -> still emergency)
        self.sensors["cpu"] = 85.0
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertTrue(self.controller.emergency)

        # Temp drops to 80C (<= emergency_c - 10) on all monitored sensors -> emergency clears!
        self.sensors["cpu"] = 80.0
        self.sensors["gpu"] = 65.0
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertFalse(self.controller.emergency)
        # Returns to requested fixed mode
        self.assertEqual(self.controller.current_effective_mode, "fixed")

    def test_emergency_deduplicates_unchanged_100_until_reassert(self) -> None:
        # Finding 7: Latch override priority but dedup unchanged 100 until reassert
        self.sensors = {"cpu": 92.0, "gpu": 50.0}
        self.controller.tick()
        self.backend.writes.clear()

        # Next tick: still emergency (92C), but target unchanged (100%) and reassert not reached
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertEqual(len(self.backend.writes), 0)

        # After reassert_s (10s), reasserts 100%
        self.clock.advance(10.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertEqual(
            self.backend.writes,
            [("set_percent", "cpu", 100), ("set_percent", "gpu", 100)],
        )

    def test_emergency_all_sources_latched(self) -> None:
        # Finding 7: third sensor (chassis) triggers emergency; remains latched when CPU/GPU cool
        self.sensors = {"cpu": 50.0, "gpu": 50.0, "chassis": 95.0}
        self.controller.tick()
        self.assertTrue(self.controller.emergency)

        # CPU/GPU cool down, chassis cools to 85C (above clear threshold 80C)
        self.sensors = {"cpu": 40.0, "gpu": 40.0, "chassis": 85.0}
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertTrue(self.controller.emergency)

        # Chassis drops to 78C (<= 80C) -> emergency clears!
        self.sensors["chassis"] = 78.0
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertFalse(self.controller.emergency)

    def test_missing_sensor_prevents_emergency_clearing(self) -> None:
        # Trip emergency
        self.sensors = {"cpu": 92.0, "gpu": 70.0}
        self.controller.tick()
        self.assertTrue(self.controller.emergency)

        # CPU drops to 75C, but GPU reading becomes None!
        # Missing cannot be assumed cool, so emergency MUST remain latched
        self.sensors = {"cpu": 75.0, "gpu": None}
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertTrue(self.controller.emergency)

        # Once GPU reading returns and is cool (<= 80C), emergency clears
        self.sensors["gpu"] = 70.0
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertFalse(self.controller.emergency)

    def test_explicit_emergency_beats_cpu_loss_auto(self) -> None:
        # CPU missing for 3 ticks (would trigger auto)
        self.sensors["cpu"] = None
        self.sensors["gpu"] = 95.0  # GPU is in emergency!
        for _ in range(3):
            self.clock.advance(2.0)
            self.wall_clock.advance(2.0)
            self.controller.tick()

        # Both CPU fault and emergency are active; emergency has highest priority!
        self.assertTrue(self.controller.cpu_fault)
        self.assertTrue(self.controller.emergency)
        self.assertEqual(self.controller.current_effective_mode, "emergency")
        self.assertEqual(self.backend.current_percents["cpu"], 100)
        self.assertEqual(self.backend.current_percents["gpu"], 100)

    def test_emergency_clear_respects_latched_cpu_fault(self) -> None:
        # Start in emergency with CPU missing
        self.sensors = {"cpu": None, "gpu": 95.0}
        for _ in range(3):
            self.clock.advance(2.0)
            self.wall_clock.advance(2.0)
            self.controller.tick()
        self.assertTrue(self.controller.cpu_fault)
        self.assertTrue(self.controller.emergency)

        # GPU cools down to 70C, but CPU is still missing
        # Missing sensor keeps emergency latched
        self.sensors["gpu"] = 70.0
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertTrue(self.controller.emergency)

        # CPU returns but reports cool temp (70C)
        self.sensors["cpu"] = 70.0
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertFalse(self.controller.emergency)
        self.assertFalse(self.controller.cpu_fault)

    def test_wallclock_gap_triggers_reapply(self) -> None:
        self.controller.set_mode("fixed", percent=60)
        self.controller.tick()
        self.backend.writes.clear()
        self.backend.open_count = 0
        self.backend.close_count = 0

        # Normal tick interval is 2.0s. Simulate a gap of 15s on wall clock
        self.clock.advance(2.0)
        self.wall_clock.advance(15.0)
        self.controller.tick()

        # Must have safely closed and reopened backend (Finding 2)
        self.assertEqual(self.backend.close_count, 1)
        self.assertEqual(self.backend.open_count, 1)

        # Must have re-applied writes to backend
        set_percent_writes = [w for w in self.backend.writes if w[0] == "set_percent"]
        self.assertEqual(
            set_percent_writes,
            [("set_percent", "cpu", 60), ("set_percent", "gpu", 60)],
        )

    def test_backend_exception_triggers_fail_auto(self) -> None:
        self.controller.set_mode("fixed", percent=60)
        self.backend.raise_on_set_percent = True
        self.controller.tick()

        # Should fall back to auto mode and record last_error
        self.assertEqual(self.controller.current_effective_mode, "auto")
        self.assertIsNotNone(self.controller.last_error)
        self.assertIn("Backend error", self.controller.last_error)

    def test_fail_auto_invalidates_caches_recovering_same_target_forces_write(self) -> None:
        # Finding 6: fail_auto must invalidate output caches so reselecting same target writes
        self.controller.set_mode("fixed", percent=50)
        self.backend.raise_on_set_percent = True
        self.controller.tick()
        self.assertEqual(self.controller.current_effective_mode, "auto")

        # Backend recovers, user selects same 50% target again
        self.backend.raise_on_set_percent = False
        self.backend.writes.clear()
        self.controller.set_mode("fixed", percent=50)
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()

        # Must write 50% immediately, not skip write due to stale cache
        self.assertEqual(
            self.backend.writes,
            [("set_percent", "cpu", 50), ("set_percent", "gpu", 50)],
        )

    def test_curve_transition_from_max_starts_at_100(self) -> None:
        # Finding 3: Entering curve from max starts smoothing at 100, not old curve or None
        self.controller.set_mode("max")
        self.controller.tick()

        self.sensors = {"cpu": 45.0, "gpu": 45.0}  # Curve target would be 25%
        self.controller.set_mode("curve")
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()

        # Must only step down by at most 10 pp from 100 -> 90%
        self.assertEqual(self.backend.current_percents["cpu"], 90)
        self.assertEqual(self.backend.current_percents["gpu"], 90)

    def test_command_spam_does_not_advance_smoothing_or_missing_counter(self) -> None:
        # Finding 3: Client commands do not advance missing counter or drop curve multiple times
        self.sensors = {"cpu": None, "gpu": 45.0}
        self.controller.tick()
        self.assertEqual(self.controller.cpu_missing_count, 1)

        # Client sends multiple commands between ticks
        for _ in range(5):
            self.controller.apply_command()
        # Missing counter stays 1
        self.assertEqual(self.controller.cpu_missing_count, 1)

    def test_invalid_sensor_values_normalized_and_trigger_cpu_fault(self) -> None:
        # Finding 5: NaN/Inf/bool treated as unusable, normalized to None, counting as CPU loss
        self.sensors = {"cpu": float("nan"), "gpu": 45.0}
        self.controller.tick()
        self.assertEqual(self.controller.cpu_missing_count, 1)
        self.assertIsNone(self.controller.last_temps["cpu"])

        self.sensors["cpu"] = float("inf")
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertEqual(self.controller.cpu_missing_count, 2)

        self.sensors["cpu"] = True
        self.clock.advance(2.0)
        self.wall_clock.advance(2.0)
        self.controller.tick()
        self.assertEqual(self.controller.cpu_missing_count, 3)
        self.assertTrue(self.controller.cpu_fault)

        # Status output JSON serialization works without error
        status = self.controller.get_status()
        json_str = json.dumps(status, allow_nan=False)
        self.assertIn('"ok": true', json_str)

    def test_telemetry_failure_in_get_status_fails_auto(self) -> None:
        # Finding 5: Unexpected backend telemetry read failure must fail auto
        self.controller.set_mode("fixed", percent=60)
        self.controller.tick()
        self.assertEqual(self.controller.current_effective_mode, "fixed")

        self.backend.raise_on_read_percent = True
        status = self.controller.get_status()
        self.assertEqual(self.controller.current_effective_mode, "auto")
        self.assertIsNotNone(self.controller.last_error)
        self.assertIn("Telemetry read error", self.controller.last_error)

    def test_set_curve_atomic_validation(self) -> None:
        old_curve = self.controller.curves["cpu"]
        # Invalid curve (non-increasing temperatures)
        with self.assertRaises(ConfigError):
            self.controller.set_curve("cpu", [[50, 30], [45, 50]])
        # Curve was not partially modified
        self.assertEqual(self.controller.curves["cpu"], old_curve)

        # Valid curve updates successfully
        new_curve = [[40, 25], [70, 70], [90, 100]]
        self.controller.set_curve("cpu", new_curve)
        self.assertEqual(len(self.controller.curves["cpu"]), 3)


if __name__ == "__main__":
    unittest.main()
