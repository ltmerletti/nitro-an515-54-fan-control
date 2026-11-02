"""Tests for nitrofan.curve -- interpolation, hysteresis, rate limiting, floor."""

import unittest

from nitrofan.config import CurvePoint
from nitrofan.curve import (
    HYSTERESIS_C,
    MAX_DOWN_PER_TICK,
    FanState,
    interpolate,
    validate_fixed_percent,
)


# -----------------------------------------------------------------------
# Standard test curve matching the spec defaults
# -----------------------------------------------------------------------

SPEC_CPU_CURVE = (
    CurvePoint(45, 25),
    CurvePoint(60, 40),
    CurvePoint(70, 60),
    CurvePoint(80, 85),
    CurvePoint(88, 100),
)

MIN_PERCENT = 25


class TestInterpolation(unittest.TestCase):
    """Test the pure interpolation function."""

    def test_at_first_point(self):
        self.assertEqual(interpolate(SPEC_CPU_CURVE, 45), 25)

    def test_at_last_point(self):
        self.assertEqual(interpolate(SPEC_CPU_CURVE, 88), 100)

    def test_below_first_clamps(self):
        self.assertEqual(interpolate(SPEC_CPU_CURVE, 20), 25)

    def test_above_last_clamps(self):
        self.assertEqual(interpolate(SPEC_CPU_CURVE, 100), 100)

    def test_midpoint_linear(self):
        # Between 45C/25% and 60C/40%: midpoint at 52.5C should give 32.5% -> 32
        result = interpolate(SPEC_CPU_CURVE, 52.5)
        # 52.5 is halfway: (52.5-45)/(60-45) = 0.5, 25 + 0.5*15 = 32.5 -> round to 32
        self.assertEqual(result, 32)

    def test_exact_internal_point(self):
        self.assertEqual(interpolate(SPEC_CPU_CURVE, 60), 40)
        self.assertEqual(interpolate(SPEC_CPU_CURVE, 70), 60)

    def test_interpolation_high_segment(self):
        # Between 80C/85% and 88C/100%: at 84C
        # frac = (84-80)/(88-80) = 0.5, 85 + 0.5*15 = 92.5 -> 92
        self.assertEqual(interpolate(SPEC_CPU_CURVE, 84), 92)

    def test_single_point_curve(self):
        curve = (CurvePoint(50, 50),)
        self.assertEqual(interpolate(curve, 40), 50)
        self.assertEqual(interpolate(curve, 50), 50)
        self.assertEqual(interpolate(curve, 60), 50)

    def test_two_point_curve(self):
        curve = (CurvePoint(40, 0), CurvePoint(80, 100))
        self.assertEqual(interpolate(curve, 40), 0)
        self.assertEqual(interpolate(curve, 80), 100)
        self.assertEqual(interpolate(curve, 60), 50)

    def test_empty_curve_raises(self):
        with self.assertRaises(ValueError):
            interpolate((), 50)

    def test_rounding_boundary(self):
        # Construct a scenario where interpolation gives exactly X.5
        # Between (0, 0) and (10, 5): at temp 5 -> frac=0.5, val=2.5 -> round to 2 (banker's)
        curve = (CurvePoint(0, 0), CurvePoint(10, 5))
        result = interpolate(curve, 5)
        self.assertEqual(result, 2)  # int(round(2.5)) = 2 (half-to-even)

    def test_rounding_boundary_odd(self):
        # Between (0, 0) and (10, 7): at temp 5 -> frac=0.5, val=3.5 -> round to 4 (half-to-even)
        curve = (CurvePoint(0, 0), CurvePoint(10, 7))
        result = interpolate(curve, 5)
        self.assertEqual(result, 4)  # int(round(3.5)) = 4


class TestHysteresis(unittest.TestCase):
    """Test hysteresis: speed drops only after temp drops HYSTERESIS_C below peak."""

    def test_holding_speed_keeps_last_raise_temperature(self):
        fan = FanState()
        self.assertEqual(fan.update(80, 80, MIN_PERCENT), 80)
        self.assertEqual(fan.update(79, 80, MIN_PERCENT), 80)
        self.assertEqual(fan.update(76, 60, MIN_PERCENT), 70)

    def test_invalid_inputs_do_not_change_fan_state(self):
        fan = FanState()
        for temperature, target in ((float("nan"), 60), (float("inf"), 60),
                                    (70, 101), (70, True)):
            with self.subTest(temperature=temperature, target=target):
                with self.assertRaises(ValueError):
                    fan.update(temperature, target, MIN_PERCENT)
                self.assertIsNone(fan.last_output)
        with self.assertRaises(ValueError):
            interpolate(SPEC_CPU_CURVE, float("nan"))

    def test_no_drop_without_hysteresis_met(self):
        """Speed should hold when temp drops less than HYSTERESIS_C."""
        fs = FanState()
        # Start at 70C -> 60%
        out1 = fs.update(70, interpolate(SPEC_CPU_CURVE, 70), MIN_PERCENT)
        self.assertEqual(out1, 60)

        # Drop to 67C (only 3C below 70, less than HYSTERESIS_C=4)
        raw = interpolate(SPEC_CPU_CURVE, 67)
        out2 = fs.update(67, raw, MIN_PERCENT)
        self.assertEqual(out2, 60)  # should hold, not drop

    def test_drop_with_hysteresis_met(self):
        """Speed should decrease once temp drops HYSTERESIS_C below high water."""
        fs = FanState()
        out1 = fs.update(70, interpolate(SPEC_CPU_CURVE, 70), MIN_PERCENT)
        self.assertEqual(out1, 60)

        # Drop to 66C (4C below 70, meets HYSTERESIS_C)
        raw = interpolate(SPEC_CPU_CURVE, 66)
        out2 = fs.update(66, raw, MIN_PERCENT)
        # Should decrease, but rate-limited: max drop is 10pp
        self.assertLess(out2, 60)
        # raw at 66C: frac=(66-60)/(70-60)=0.6, 40+0.6*20=52 -> 52
        # 60-10=50, max(52,50)=52
        self.assertEqual(out2, 52)

    def test_immediate_raise(self):
        """Speed increases take effect immediately, no hysteresis delay."""
        fs = FanState()
        fs.update(50, interpolate(SPEC_CPU_CURVE, 50), MIN_PERCENT)
        # Jump to 80C
        out = fs.update(80, interpolate(SPEC_CPU_CURVE, 80), MIN_PERCENT)
        self.assertEqual(out, 85)

    def test_gradual_cooling(self):
        """Simulate gradual cooling from 80C to 50C with 2C steps."""
        fs = FanState()
        fs.update(80, interpolate(SPEC_CPU_CURVE, 80), MIN_PERCENT)

        # Cool gradually -- speed should hold until hysteresis met, then step down
        outputs = []
        for temp in range(78, 48, -2):
            raw = interpolate(SPEC_CPU_CURVE, temp)
            out = fs.update(temp, raw, MIN_PERCENT)
            outputs.append((temp, raw, out))

        # Speed should be non-increasing
        speeds = [o[2] for o in outputs]
        for i in range(1, len(speeds)):
            self.assertLessEqual(speeds[i], speeds[i - 1],
                                 f"speed increased at step {i}: {outputs[i-1]} -> {outputs[i]}")

    def test_immediate_reheat(self):
        """After cooling, reheating should raise speed immediately."""
        fs = FanState()
        fs.update(80, interpolate(SPEC_CPU_CURVE, 80), MIN_PERCENT)

        # Cool enough to trigger decrease
        for temp in range(78, 60, -2):
            fs.update(temp, interpolate(SPEC_CPU_CURVE, temp), MIN_PERCENT)

        current = fs.last_output
        # Reheat to 82C
        out = fs.update(82, interpolate(SPEC_CPU_CURVE, 82), MIN_PERCENT)
        self.assertGreater(out, current)


class TestRateLimiting(unittest.TestCase):
    """Test downward rate limiting (MAX_DOWN_PER_TICK pp per tick)."""

    def test_large_drop_is_rate_limited(self):
        """A large target decrease should only drop MAX_DOWN_PER_TICK per tick."""
        fs = FanState()
        fs.update(85, interpolate(SPEC_CPU_CURVE, 85), MIN_PERCENT)
        # Sudden temp drop well past hysteresis
        raw = interpolate(SPEC_CPU_CURVE, 45)  # 25%
        out = fs.update(45, raw, MIN_PERCENT)
        # From ~94 (at 85C), can only drop 10 -> 84
        # Actually at 85C: frac=(85-80)/(88-80)=0.625, 85+0.625*15=94.375 -> 94
        self.assertGreaterEqual(out, fs.last_output)  # trivially true since we just set it
        # The key check: first tick can't jump all the way to 25
        # Re-test from known state
        fs2 = FanState()
        fs2.update(85, 94, MIN_PERCENT)
        out2 = fs2.update(45, 25, MIN_PERCENT)
        self.assertEqual(out2, 84)  # 94 - 10

    def test_upward_is_unlimited(self):
        """Upward speed changes have no rate limit."""
        fs = FanState()
        fs.update(45, 25, MIN_PERCENT)
        # Jump from 25% to 100% in one tick
        out = fs.update(88, 100, MIN_PERCENT)
        self.assertEqual(out, 100)

    def test_multiple_ticks_gradual_decrease(self):
        """Speed should step down at most MAX_DOWN_PER_TICK per tick."""
        fs = FanState()
        fs.update(85, 90, MIN_PERCENT)
        # Keep temp well below hysteresis threshold
        results = []
        for _ in range(10):
            out = fs.update(30, 25, MIN_PERCENT)
            results.append(out)
        # Each step drops at most 10
        for i in range(1, len(results)):
            drop = results[i - 1] - results[i]
            self.assertLessEqual(drop, MAX_DOWN_PER_TICK)
        # Eventually reaches 25 (the min_percent floor)
        self.assertEqual(results[-1], 25)


class TestFloorEnforcement(unittest.TestCase):
    """Test that min_percent floor is enforced on curve output."""

    def test_curve_below_floor_gets_raised(self):
        """Curve output below min_percent should be raised to min_percent."""
        # Curve with low starting percent
        curve = (CurvePoint(40, 10), CurvePoint(80, 100))
        fs = FanState()
        raw = interpolate(curve, 40)  # 10%
        out = fs.update(40, raw, 30)  # min_percent=30
        self.assertEqual(out, 30)

    def test_curve_above_floor_unchanged(self):
        curve = (CurvePoint(40, 50), CurvePoint(80, 100))
        fs = FanState()
        raw = interpolate(curve, 40)  # 50%
        out = fs.update(40, raw, 30)  # min_percent=30
        self.assertEqual(out, 50)


class TestIndependentFanState(unittest.TestCase):
    """Verify that fan states are independent per-fan."""

    def test_two_fans_independent(self):
        cpu = FanState()
        gpu = FanState()

        # CPU at high temp, GPU at low temp
        cpu_out = cpu.update(80, 85, MIN_PERCENT)
        gpu_out = gpu.update(45, 25, MIN_PERCENT)

        self.assertEqual(cpu_out, 85)
        self.assertEqual(gpu_out, 25)

        # GPU heats up, CPU stays the same
        cpu_out2 = cpu.update(80, 85, MIN_PERCENT)
        gpu_out2 = gpu.update(80, 85, MIN_PERCENT)

        self.assertEqual(cpu_out2, 85)
        self.assertEqual(gpu_out2, 85)

    def test_reset(self):
        fs = FanState()
        fs.update(70, 60, MIN_PERCENT)
        fs.reset()
        self.assertIsNone(fs.last_output)
        self.assertIsNone(fs.high_water_temp)


class TestValidateFixedPercent(unittest.TestCase):
    """Test the fixed-percent validator."""

    def test_valid_percent(self):
        self.assertEqual(validate_fixed_percent(50, 25), 50)

    def test_at_minimum(self):
        self.assertEqual(validate_fixed_percent(25, 25), 25)

    def test_at_100(self):
        self.assertEqual(validate_fixed_percent(100, 25), 100)

    def test_zero_rejected(self):
        with self.assertRaises(ValueError) as cm:
            validate_fixed_percent(0, 25)
        self.assertIn("0%", str(cm.exception))

    def test_below_minimum_rejected(self):
        with self.assertRaises(ValueError):
            validate_fixed_percent(10, 25)

    def test_boolean_rejected(self):
        with self.assertRaises(ValueError) as cm:
            validate_fixed_percent(True, 25)  # type: ignore[arg-type]
        self.assertIn("bool", str(cm.exception))

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            validate_fixed_percent(-5, 25)

    def test_over_100_rejected(self):
        with self.assertRaises(ValueError):
            validate_fixed_percent(105, 25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
