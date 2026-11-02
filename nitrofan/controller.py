"""
controller.py - Deterministic fan speed controller and safety logic.

Coordinates sensor readings, fan curves, rate limiting, and hardware/mock
backends. Implements all NitroFan safety constraints:
- Emergency override (100% on high temperature with 10C hysteresis)
- Sensor failure detection (3 consecutive missing CPU readings force auto)
- Wall clock gap detection across sleep/suspend to reassert mode
- Floor speed enforcement and validation on all manual modes
- Reassertion and change-only backend writes
- Clean recovery and fail-safe fallback to automatic mode
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from typing import Any

from nitrofan.backends.base import Backend
from nitrofan.config import Config, CurvePoint, parse_curve_points
from nitrofan.curve import FanState, interpolate, validate_fixed_percent
from nitrofan.protocol import MAX_FRAME_BYTES, encode_reply

logger = logging.getLogger(__name__)

# Leave room for temperatures, fan telemetry, and errors in every status frame.
MAX_CURVE_BYTES = MAX_FRAME_BYTES // 2


def _check_curve_budget(curves: Mapping[str, tuple[CurvePoint, ...]]) -> None:
    data = {fan: [[point.temp_c, point.percent] for point in points]
            for fan, points in curves.items()}
    if len(encode_reply(True, curves=data)) > MAX_CURVE_BYTES:
        raise ValueError("Combined curves are too large for status replies (32 KiB limit)")


def _normalize_temp(val: Any) -> float | None:
    """
    Normalize sensor reading to a finite float or None.

    Rejects bool, NaN, Infinity, and non-numeric values without raising.
    """
    if val is None or isinstance(val, bool):
        return None
    if not isinstance(val, (int, float)):
        return None
    try:
        f = float(val)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(f):
        return None
    return f


class Controller:
    """
    Deterministic fan controller.

    Evaluates fan speeds on each tick without sleep calls or background
    threads, enabling deterministic unit testing via injected clocks,
    sensors, and backends.
    """

    def __init__(
        self,
        config: Config,
        backend: Backend,
        sensor_provider: Callable[[], Mapping[str, float | None]],
        clock: Callable[[], float],
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.backend = backend
        self.sensor_provider = sensor_provider
        self.clock = clock
        self.wall_clock = wall_clock if wall_clock is not None else clock

        # Curves per fan (mutable mapping until restart)
        self.curves: dict[str, tuple[CurvePoint, ...]] = {
            fan: points for fan, points in config.curves.fans.items()
        }
        _check_curve_budget(self.curves)

        # Per-fan state tracking
        self.fan_states: dict[str, FanState] = {
            fan: FanState() for fan in backend.fans
        }
        self.fixed_percents: dict[str, int] = {
            fan: config.min_percent for fan in backend.fans
        }

        # Modes and safety states
        self.requested_mode: str = config.startup_mode
        self.current_effective_mode: str = config.startup_mode
        if getattr(self.backend, "read_only", False) and self.requested_mode != "auto":
            raise ValueError(
                f"Cannot initialize read-only backend with manual startup_mode '{self.requested_mode}'; "
                "read-only mode supports 'auto' (monitoring) only"
            )
        self.emergency: bool = False
        self._emergency_sources: set[str] = set()
        self.cpu_missing_count: int = 0
        self.cpu_fault: bool = False
        self.last_error: str | None = None

        # Telemetry and write caches
        self.last_temps: dict[str, float | None] = {}
        self.last_target_percents: dict[str, int | None] = {
            fan: None for fan in backend.fans
        }
        self.last_write_times: dict[str, float] = {
            fan: -1e9 for fan in backend.fans
        }
        self.last_auto_reassert_time: float = -1e9
        self.last_tick_time: float | None = None
        self.last_wall_time: float | None = None

        self.start_time: float = self.clock()

    def set_mode(
        self,
        mode: str,
        percent: int | Mapping[str, int] | None = None,
        percents: Mapping[str, int] | None = None,
    ) -> None:
        """
        Request a mode change (auto, max, fixed, curve).

        Validates the entire request atomically before touching any state.
        Manual requests cannot clear emergency overrides or CPU fault states.
        """
        valid_modes = ("auto", "max", "fixed", "curve")
        if mode not in valid_modes:
            raise ValueError(f"Unknown mode '{mode}'; valid modes are {valid_modes}")

        if getattr(self.backend, "read_only", False) and mode != "auto":
            raise ValueError("Cannot set manual mode on read-only backend")

        if self.cpu_fault and mode != "auto":
            raise ValueError("Cannot set manual mode while CPU sensor fault is active")

        if mode == "fixed":
            if percent is not None and percents is not None:
                if percent != percents:
                    raise ValueError("Contradictory 'percent' and 'percents' provided")
            payload = percent if percent is not None else percents
            if payload is None:
                raise ValueError("fixed mode requires 'percent' integer or mapping")

            new_fixed: dict[str, int] = {}
            if isinstance(payload, int) and not isinstance(payload, bool):
                validated = validate_fixed_percent(payload, self.config.min_percent)
                new_fixed = {fan: validated for fan in self.backend.fans}
            elif isinstance(payload, Mapping):
                for fan in self.backend.fans:
                    if fan not in payload:
                        raise ValueError(f"Missing fixed speed for fan '{fan}'")
                for fan, val in payload.items():
                    if fan not in self.backend.fans:
                        raise ValueError(f"Unknown fan '{fan}' in fixed speed map")
                    new_fixed[fan] = validate_fixed_percent(val, self.config.min_percent)
            else:
                raise ValueError(
                    f"fixed mode requires integer or mapping, got {type(payload).__name__}"
                )

            if getattr(self.backend, "name", "") == "nbfc":
                speeds = list(new_fixed.values())
                if len(set(speeds)) > 1:
                    raise ValueError("nbfc backend requires equal combined fan speeds")
                for target_pct in speeds:
                    if target_pct < 25 or target_pct > 100:
                        raise ValueError(f"nbfc backend requires fixed percent in range 25..100, got {target_pct}")

            self.fixed_percents = new_fixed
            self.requested_mode = "fixed"
            logger.info("Mode changed to fixed: %s", self.fixed_percents)
            return

        if mode == "curve":
            if getattr(self.backend, "can_curve", True) is False or getattr(self.backend, "name", "") == "nbfc":
                raise ValueError("nbfc backend does not support software curves")
            missing = [f for f in self.backend.fans if f not in self.curves or not self.curves[f]]
            if missing:
                raise ValueError(f"Cannot switch to curve mode: missing curves for fans {missing}")
            if self.requested_mode != "curve":
                for fan in self.backend.fans:
                    self._seed_curve_state(fan)
            self.requested_mode = "curve"
            logger.info("Mode changed to curve")
            return

        if mode == "max":
            self.requested_mode = "max"
            for fan in self.backend.fans:
                self.fan_states[fan].last_output = 100
            logger.info("Mode changed to max")
            return

        if mode == "auto":
            self.requested_mode = "auto"
            logger.info("Mode changed to auto")
            return

    def set_curve(self, fan: str, points: list[Any]) -> None:
        """
        Update the fan curve for a single fan until daemon restart.

        Validates points using shared config parsing rules before making any changes.
        """
        if getattr(self.backend, "read_only", False):
            raise ValueError("Cannot modify fan curves on read-only backend")
        if getattr(self.backend, "can_curve", True) is False or getattr(self.backend, "name", "") == "nbfc":
            raise ValueError("nbfc backend does not support software curves")
        if fan not in self.backend.fans:
            raise ValueError(f"Unknown fan '{fan}'; available: {self.backend.fans}")
        parsed = parse_curve_points(points, f"curves.{fan}")
        _check_curve_budget({**self.curves, fan: parsed})
        self.curves[fan] = parsed
        self._seed_curve_state(fan)
        logger.info("Curve updated for fan '%s' (%d points)", fan, len(parsed))

    def _seed_curve_state(self, fan: str) -> None:
        """Keep the applied speed while allowing a new curve to cool one step per tick."""
        state = self.fan_states[fan]
        state.reset()
        previous = self.last_target_percents[fan]
        if previous is not None:
            state.last_output = previous
            # This baseline came from a user command, rather than a temperature
            # rise. The next curve-driven increase supplies its hysteresis reference.

    def _fail_auto(self) -> None:
        """Fallback to auto mode on errors, invalidating output caches."""
        self.requested_mode = "auto"
        self.current_effective_mode = "auto"
        for fan in self.backend.fans:
            self.last_target_percents[fan] = None
            self.last_write_times[fan] = -1e9
            self.fan_states[fan].reset()
        self.last_auto_reassert_time = -1e9
        if not getattr(self.backend, "read_only", False):
            try:
                self.backend.set_auto()
            except Exception as e:
                logger.error("set_auto threw exception during fail_auto: %s", e)

    def _reapply(self, now: float) -> None:
        """
        Force reassertion of backend state after a clock gap or suspend.

        Preserves safety latches and counters while marking targets dirty.
        """
        for fan in self.backend.fans:
            self.last_write_times[fan] = -1e9
            self.last_target_percents[fan] = None
        self.last_auto_reassert_time = -1e9

    def _compute_effective_targets(self, is_periodic: bool) -> tuple[str, dict[str, int] | None]:
        """
        Compute effective mode and target percents based on latest telemetry and latches.
        """
        if getattr(self.backend, "read_only", False):
            # Read-only backend stays in auto monitoring mode without fan speed targets
            return "auto", None

        if self.emergency:
            effective_mode = "emergency"
            targets: dict[str, int] | None = {fan: 100 for fan in self.backend.fans}
            for fan in self.backend.fans:
                self.fan_states[fan].last_output = 100
                self.fan_states[fan].high_water_temp = float(self.config.emergency_c)
        elif self.cpu_fault:
            effective_mode = "auto"
            targets = None
        else:
            effective_mode = self.requested_mode
            if effective_mode == "auto":
                targets = None
            elif effective_mode == "max":
                targets = {fan: 100 for fan in self.backend.fans}
                for fan in self.backend.fans:
                    self.fan_states[fan].last_output = 100
            elif effective_mode == "fixed":
                targets = {fan: self.fixed_percents[fan] for fan in self.backend.fans}
                for fan in self.backend.fans:
                    self.fan_states[fan].last_output = self.fixed_percents[fan]
            elif effective_mode == "curve":
                targets = {}
                for fan in self.backend.fans:
                    curve = self.curves.get(fan)
                    temp = self.last_temps.get(fan)
                    state = self.fan_states[fan]
                    if curve is None or temp is None:
                        base = state.last_output if state.last_output is not None else self.config.min_percent
                        target = max(base, self.config.min_percent)
                        state.last_output = target
                        targets[fan] = target
                    else:
                        raw = interpolate(curve, temp)
                        if is_periodic:
                            target = state.update(temp, raw, self.config.min_percent)
                        else:
                            # Immediate command evaluation: increases are immediate,
                            # decreases deferred to periodic tick to prevent spam lowering
                            if state.last_output is None:
                                state.last_output = self.config.min_percent
                            if raw > state.last_output:
                                target = state.update(temp, raw, self.config.min_percent)
                            else:
                                target = max(state.last_output, self.config.min_percent)
                                state.last_output = target
                        targets[fan] = target
            else:
                targets = None

        return effective_mode, targets

    def _apply_backend_targets(
        self,
        effective_mode: str,
        targets: dict[str, int] | None,
        now: float,
    ) -> bool:
        """Apply computed targets to backend using change-only and reassert logic."""
        if getattr(self.backend, "read_only", False):
            self.current_effective_mode = "auto"
            return True

        if targets is None:
            needs_auto = (
                self.current_effective_mode != "auto"
                or (now - self.last_auto_reassert_time >= self.config.reassert_s)
            )
            if needs_auto:
                try:
                    self.backend.set_auto()
                    self.last_auto_reassert_time = now
                    self.last_target_percents = {fan: None for fan in self.backend.fans}
                except Exception as e:
                    logger.error("Failed to set_auto on backend: %s", e)
                    self.last_error = f"Backend error: {e}"
                    self._fail_auto()
                    return False
        else:
            to_write = {
                fan: pct
                for fan, pct in targets.items()
                if self.last_target_percents.get(fan) != pct
                or (now - self.last_write_times.get(fan, -1e9) >= self.config.reassert_s)
            }
            if to_write:
                try:
                    self.backend.set_targets(to_write)
                    for fan, pct in to_write.items():
                        self.last_target_percents[fan] = pct
                        self.last_write_times[fan] = now
                except Exception as e:
                    logger.error("Failed to set_targets(%s): %s", to_write, e)
                    self.last_error = f"Backend error: {e}"
                    self._fail_auto()
                    return False

        self.current_effective_mode = effective_mode
        return True

    def apply_command(self) -> bool:
        """
        Immediately apply mode or curve changes using latest sampled temperatures
        and existing safety latches, without advancing periodic counters or simulation.
        """
        now = self.clock()
        try:
            effective_mode, targets = self._compute_effective_targets(is_periodic=False)
            return self._apply_backend_targets(effective_mode, targets, now)
        except Exception as e:
            logger.exception("Error applying command: %s", e)
            self.last_error = f"Control error: {e}"
            self._fail_auto()
            return False

    def tick(self) -> None:
        """
        Execute one deterministic control step.

        Evaluates sensor readings, enforces safety constraints, updates fan
        speeds, and writes to the backend if needed.
        """
        now = self.clock()
        now_wall = self.wall_clock()
        health_check = getattr(self.backend, "check_health", None)
        if health_check is not None:
            try:
                health_check()
            except Exception as error:
                self.last_error = f"Backend health failure: {error}"
                self._fail_auto()
                return

        # Safety 7: Detect wall clock gap (> 3 * tick_s) across actual ticks
        if self.last_wall_time is not None:
            gap = now_wall - self.last_wall_time
            if gap > 3.0 * self.config.tick_s:
                logger.warning(
                    "Wall clock gap detected: %.2fs > 3*tick_s (%.2fs). Reopening backend and reapplying mode.",
                    gap, 3.0 * self.config.tick_s
                )
                try:
                    self.backend.close()
                except Exception as e:
                    logger.warning("Error closing backend on resume: %s", e)
                try:
                    self.backend.open()
                except Exception as e:
                    logger.error("Error reopening backend on resume: %s", e)
                    self.last_error = f"Backend resume error: {e}"
                    self._fail_auto()
                    self.last_wall_time = now_wall
                    self.last_tick_time = now
                    return
                self._reapply(now)
        self.last_wall_time = now_wall
        self.last_tick_time = now

        # Read sensor values with centralized fail-safe handling
        try:
            raw_temps = self.sensor_provider()
            if not isinstance(raw_temps, Mapping):
                raise ValueError(f"sensor_provider must return a Mapping, got {type(raw_temps).__name__}")
            temps: dict[str, float | None] = {
                k: _normalize_temp(v) for k, v in raw_temps.items()
            }
            self.last_temps = temps
        except Exception as e:
            logger.exception("Failed to read sensors during tick: %s", e)
            self.last_error = f"Sensor error: {e}"
            self._fail_auto()
            return

        try:
            # Safety 2: CPU temperature sensor loss check
            cpu_temp = temps.get("cpu")
            if cpu_temp is None:
                self.cpu_missing_count += 1
                if self.cpu_missing_count >= 3:
                    if not self.cpu_fault:
                        logger.info("Switching requested mode to auto due to CPU sensor fault")
                    self.cpu_fault = True
                    self.requested_mode = "auto"
                    self.last_error = "CPU temperature missing for 3 consecutive ticks; forced auto mode"
            else:
                self.cpu_missing_count = 0
                self.cpu_fault = False
                if self.last_error and "CPU temperature missing" in self.last_error:
                    self.last_error = None

            # Safety 3: Emergency high-temperature check with 10C hysteresis across all sources
            emergency_c = float(self.config.emergency_c)
            clear_c = emergency_c - 10.0
            hot_sources = {
                src for src, val in temps.items()
                if val is not None and val >= emergency_c
            }

            if hot_sources:
                if not self.emergency:
                    action = (
                        "Read-only monitoring; firmware remains in control."
                        if self.backend.read_only else "Forcing 100% fan speed."
                    )
                    logger.warning(
                        "Emergency temperature reached on %s (>= %.1f C). %s",
                        sorted(hot_sources), emergency_c, action
                    )
                self.emergency = True
                self._emergency_sources.update(hot_sources)
                if not getattr(self.backend, "read_only", False):
                    for fan in self.backend.fans:
                        self.fan_states[fan].last_output = 100
                        self.fan_states[fan].high_water_temp = emergency_c
            elif self.emergency:
                # Latch until all relevant sensors are present and <= clear_c.
                # Neutral fan names (fan1/fan2) are tachometers and must not break emergency clearing.
                # Only include backend fan identifiers if they match actual temperature keys in temps or ("cpu", "gpu").
                if getattr(self.backend, "read_only", False):
                    monitored = set(self._emergency_sources)
                else:
                    fan_temp_sources = {f for f in self.backend.fans if f in temps or f in ("cpu", "gpu")}
                    monitored = fan_temp_sources | self._emergency_sources

                all_present = all(temps.get(src) is not None for src in monitored)
                all_cool = all(
                    temps.get(src) is not None and temps[src] <= clear_c
                    for src in monitored
                )
                if all_present and all_cool:
                    logger.info(
                        "All temperatures dropped below clear threshold (%.1f C). Clearing emergency.",
                        clear_c
                    )
                    self.emergency = False
                    self._emergency_sources.clear()

            effective_mode, targets = self._compute_effective_targets(is_periodic=True)
            self._apply_backend_targets(effective_mode, targets, now)

        except Exception as e:
            logger.exception("Error during control calculation in tick: %s", e)
            self.last_error = f"Control error: {e}"
            self._fail_auto()

    def get_status(self) -> dict[str, Any]:
        """
        Compile daemon status dictionary matching the protocol schema.
        """
        now = self.clock()
        fans_dict: dict[str, dict[str, int | None]] = {}
        try:
            for fan in self.backend.fans:
                pct = self.backend.read_percent(fan)
                fans_dict[fan] = {
                    "percent": pct,
                    "requested_percent": pct,
                    "rpm": self.backend.read_rpm(fan),
                }
        except Exception as e:
            logger.error("Backend telemetry read failed: %s", e)
            self.last_error = f"Telemetry read error: {e}"
            self._fail_auto()
            fans_dict = {
                fan: {"percent": None, "requested_percent": None, "rpm": None}
                for fan in self.backend.fans
            }

        read_only = bool(getattr(self.backend, "read_only", False))
        backend_name = str(getattr(self.backend, "name", self.config.backend))

        backend_emergency = bool(getattr(self.backend, 'emergency', False))
        status = {
            "ok": True,
            "mode": "emergency" if backend_emergency else self.current_effective_mode,
            "requested_mode": self.requested_mode,
            "emergency": self.emergency or backend_emergency,
            "backend": backend_name,
            "read_only": read_only,
            "can_control": not read_only,
            "percent_kind": "requested_target" if backend_name == "nbfc" else "backend_readback",
            "temps": dict(self.last_temps),
            "fans": fans_dict,
            "last_error": self.last_error,
            "uptime": round(now - self.start_time, 2),
            "curves": {
                fan: [[p.temp_c, p.percent] for p in points]
                for fan, points in self.curves.items()
            },
        }
        if backend_name == 'nbfc':
            status['safety'] = dict(getattr(self.backend, 'safety_status', {}))
        return status

