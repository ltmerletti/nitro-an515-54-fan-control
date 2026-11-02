"""
curve.py - Fan curve interpolation with hysteresis and rate limiting.

Pure functions plus a small FanState class that tracks per-fan hysteresis
and smoothing state. No I/O, no globals, no daemon logic.

Interpolation: linear between ordered (temp_c, percent) points. Temps below
the first point clamp to the first point's percent. Temps above the last
point clamp to the last point's percent.

Hysteresis: speed only decreases when temp drops at least HYSTERESIS_C (4)
degrees below the temperature that last caused a speed increase. Speed
increases take effect immediately.

Rate limiting: downward speed changes are limited to MAX_DOWN_PER_TICK (10)
percentage points per tick. Upward changes are unlimited.

Rounding: interpolated percent is rounded with int(round(x)) -- standard
half-to-even rounding on the float result. This gives deterministic integer
targets from fractional interpolation.

Floor enforcement: manual mode percent must be >= min_percent. Fixed 0 is
always rejected (the caller must reject it). For dynamic curve output that
lands below min_percent, the floor is applied silently because the curve is
a continuous function and crossing the floor boundary during gradual cooling
is expected. This is safe: the floor raises the speed, never lowers it.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from nitrofan.config import CurvePoint


# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------

HYSTERESIS_C = 4
MAX_DOWN_PER_TICK = 10


# -----------------------------------------------------------------------
# Pure interpolation
# -----------------------------------------------------------------------

def interpolate(points: tuple[CurvePoint, ...] | list[CurvePoint],
                temp_c: float) -> int:
    """
    Linearly interpolate fan percent from a sorted curve at the given temp.

    Returns an integer percent (0..100). Clamps outside the curve endpoints.
    Points must be sorted by temp_c ascending with at least one entry.
    """
    if not points:
        raise ValueError("curve must have at least one point")
    if isinstance(temp_c, bool) or not math.isfinite(temp_c):
        raise ValueError("temperature must be finite")

    # Below first point
    if temp_c <= points[0].temp_c:
        return points[0].percent

    # Above last point
    if temp_c >= points[-1].temp_c:
        return points[-1].percent

    # Find surrounding points and interpolate
    for i in range(len(points) - 1):
        lo = points[i]
        hi = points[i + 1]
        if lo.temp_c <= temp_c <= hi.temp_c:
            span_t = hi.temp_c - lo.temp_c
            span_p = hi.percent - lo.percent
            frac = (temp_c - lo.temp_c) / span_t
            return int(round(lo.percent + frac * span_p))

    # Should not reach here with valid sorted points, but be safe
    return points[-1].percent


# -----------------------------------------------------------------------
# Per-fan state for hysteresis and smoothing
# -----------------------------------------------------------------------

@dataclass
class FanState:
    """
    Mutable per-fan state for hysteresis and rate-limited smoothing.

    Create one per fan. Call update() each tick with the raw interpolated
    target. The returned value has hysteresis and rate limiting applied.

    last_output: the percent that was last sent to the fan (or None on first tick)
    high_water_temp: the temperature that last caused a speed increase.
        Speed can only decrease once temp drops HYSTERESIS_C below this value.
        None allows rate-limited cooling from an explicitly seeded manual speed
        until a temperature-driven increase establishes a reference.
    """
    last_output: int | None = None
    high_water_temp: float | None = None

    def update(self, temp_c: float, raw_target: int, min_percent: int) -> int:
        """
        Apply hysteresis, rate limiting, and floor to a raw interpolated target.

        Returns the final integer percent to apply.
        """
        if isinstance(temp_c, bool) or not math.isfinite(temp_c):
            raise ValueError("temperature must be finite")
        for name, value in (("target", raw_target), ("minimum", min_percent)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                raise ValueError(f"{name} percent must be an integer in 0..100")
        raw_target = max(raw_target, min_percent)
        if self.last_output is None:
            # First tick: accept target directly, apply floor
            result = max(raw_target, min_percent)
            self.last_output = result
            self.high_water_temp = temp_c
            return result

        # Hysteresis: decide whether this is an upward or downward request
        if raw_target > self.last_output:
            # Only an increase resets the hysteresis reference temperature.
            self.high_water_temp = temp_c
            effective = raw_target
        elif raw_target == self.last_output:
            effective = self.last_output
        else:
            # Lowering: only allow if temp dropped enough below the temp
            # that last *raised* speed (high_water_temp).
            if self.high_water_temp is None or temp_c <= self.high_water_temp - HYSTERESIS_C:
                # Temp has dropped enough; allow decrease with rate limit
                effective = max(raw_target, self.last_output - MAX_DOWN_PER_TICK)
                # Do NOT update high_water_temp on decrease. It stays at the
                # temp of the last raise so continued cooling at the same or
                # lower temp keeps the hysteresis gate open.
            else:
                # Temp hasn't dropped enough; hold current speed
                effective = self.last_output

        # Floor enforcement for dynamic curve output
        result = max(effective, min_percent)
        self.last_output = result
        return result

    def reset(self) -> None:
        """Reset state, for example when switching modes."""
        self.last_output = None
        self.high_water_temp = None


def validate_fixed_percent(percent: int, min_percent: int) -> int:
    """
    Validate a fixed-speed percent request.

    Rejects 0 (never allow fans off in manual mode). Rejects values below
    min_percent. Returns the validated percent on success.

    Raises ValueError with a clear message on rejection.
    """
    if not isinstance(percent, int) or isinstance(percent, bool):
        raise ValueError(f"fixed percent must be integer, got {type(percent).__name__}")
    if percent == 0:
        raise ValueError("fixed 0% rejected: fans must not be turned off in manual mode")
    if percent < min_percent:
        raise ValueError(
            f"fixed {percent}% below minimum {min_percent}%"
        )
    if percent < 0 or percent > 100:
        raise ValueError(f"fixed {percent}% out of range 1..100")
    return percent
