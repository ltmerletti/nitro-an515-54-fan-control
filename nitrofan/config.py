"""
config.py - TOML configuration loader and validator for NitroFan.

Reads /etc/nitrofan/config.toml (or a caller-specified path) with tomllib.
Validates every field, checks writable EC registers against the allowlist,
and returns a frozen Config object. Bad config raises ConfigError with a
clear message; no partial mutation occurs.

This module never touches hardware, loads kernel modules, or writes files.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

DEFAULT_CONFIG_PATH = "/etc/nitrofan/config.toml"

VALID_BACKENDS = ("ec", "hwmon", "mock", "nbfc")
VALID_STARTUP_MODES = ("auto", "max", "curve")
SUPPORTED_EC_RPM_ENCODINGS = ("u16_le",)


class ConfigError(Exception):
    """Raised when config is invalid or missing required fields."""


# -----------------------------------------------------------------------
# Data classes (all frozen -- no accidental mutation after validation)
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class NBFCConfig:
    """Settings for the NBFC guarded backend."""
    manifest_path: str = "/opt/nitrofan/build/manifest.json"
    runtime_dir: str = "/opt/nitrofan/runtime"
    policy_path: str | None = None
    profile_path: str | None = None
    service_bin: str | None = None
    ec_path: str | None = None
    sysfs_root: str | None = None
    synthetic_root: str | None = None


@dataclass(frozen=True)
class ECRpmFan:
    """Configured EC RPM tachometer registers and encoding for one fan."""
    low_register: int
    high_register: int
    encoding: str = "u16_le"


@dataclass(frozen=True)
class FanEC:
    """EC register layout for one fan. All values come from config, not code."""
    mode_register: int
    mode_auto: int
    mode_manual: int
    speed_register: int
    speed_max: int
    read_register: int


@dataclass(frozen=True)
class ECConfig:
    """EC backend settings including the unlock sequence and per-fan maps."""
    unlock_register: int
    unlock_value: int
    write_allowlist: tuple[int, ...]
    fans: Mapping[str, FanEC]


@dataclass(frozen=True)
class CurvePoint:
    """One (temp_c, percent) point in a fan curve."""
    temp_c: float
    percent: int


@dataclass(frozen=True)
class CurvesConfig:
    """Per-fan temperature curves. Keys are fan names like 'cpu', 'gpu'."""
    fans: Mapping[str, tuple[CurvePoint, ...]]


@dataclass(frozen=True)
class Config:
    """Top-level validated configuration."""
    backend: str
    tick_s: float
    reassert_s: float
    min_percent: int
    emergency_c: int
    startup_mode: str
    ec: ECConfig | None
    curves: CurvesConfig
    ec_rpm: Mapping[str, ECRpmFan] | None = None
    nbfc: NBFCConfig | None = None


# -----------------------------------------------------------------------
# Validation helpers
# -----------------------------------------------------------------------

def _require(data: dict, key: str, context: str) -> Any:
    """Return data[key] or raise ConfigError."""
    if key not in data:
        raise ConfigError(f"{context}: missing required key '{key}'")
    return data[key]


def _int_field(data: dict, key: str, context: str, *, lo: int | None = None,
               hi: int | None = None) -> int:
    """Extract and validate an integer field. Rejects bools and non-finite."""
    val = _require(data, key, context)
    # TOML bools are distinct from ints, but guard against accidental coercion
    if isinstance(val, bool):
        raise ConfigError(f"{context}.{key}: expected integer, got boolean")
    if not isinstance(val, int):
        raise ConfigError(f"{context}.{key}: expected integer, got {type(val).__name__}")
    if lo is not None and val < lo:
        raise ConfigError(f"{context}.{key}: {val} < minimum {lo}")
    if hi is not None and val > hi:
        raise ConfigError(f"{context}.{key}: {val} > maximum {hi}")
    return val


def _float_field(data: dict, key: str, context: str, *, positive: bool = False) -> float:
    """Extract and validate a numeric field as float. Rejects non-finite."""
    val = _require(data, key, context)
    if isinstance(val, bool):
        raise ConfigError(f"{context}.{key}: expected number, got boolean")
    if not isinstance(val, (int, float)):
        raise ConfigError(f"{context}.{key}: expected number, got {type(val).__name__}")
    try:
        fval = float(val)
    except OverflowError as error:
        raise ConfigError(f"{context}.{key}: number is too large") from error
    if not math.isfinite(fval):
        raise ConfigError(f"{context}.{key}: must be finite, got {val}")
    if positive and fval <= 0:
        raise ConfigError(f"{context}.{key}: must be positive, got {val}")
    return fval


def _enum_field(data: dict, key: str, context: str, allowed: tuple[str, ...]) -> str:
    """Extract and validate a string enum field."""
    val = _require(data, key, context)
    if not isinstance(val, str):
        raise ConfigError(f"{context}.{key}: expected string, got {type(val).__name__}")
    if val not in allowed:
        raise ConfigError(f"{context}.{key}: '{val}' not in {list(allowed)}")
    return val


def _byte_offset(val: Any, context: str) -> int:
    """Validate a value is an integer in 0x00..0xFF."""
    if isinstance(val, bool):
        raise ConfigError(f"{context}: expected byte offset, got boolean")
    if not isinstance(val, int):
        raise ConfigError(f"{context}: expected byte offset integer, got {type(val).__name__}")
    if not 0 <= val <= 0xFF:
        raise ConfigError(f"{context}: byte offset {val:#x} out of range 0x00..0xFF")
    return val


def _validate_writable_register(reg: int, allowlist: tuple[int, ...],
                                 context: str) -> None:
    """Reject a writable register not in the allowlist."""
    if reg not in allowlist:
        raise ConfigError(
            f"{context}: register {reg:#04x} not in write_allowlist "
            f"{[f'{r:#04x}' for r in allowlist]}"
        )


# -----------------------------------------------------------------------
# Section parsers
# -----------------------------------------------------------------------

def _parse_fan_ec(data: dict, fan_name: str, allowlist: tuple[int, ...]) -> FanEC:
    """Parse and validate one [ec.fans.<name>] section."""
    ctx = f"ec.fans.{fan_name}"
    if not isinstance(data, dict):
        raise ConfigError(f"{ctx}: expected table, got {type(data).__name__}")

    mode_register = _byte_offset(_require(data, "mode_register", ctx), f"{ctx}.mode_register")
    mode_auto = _byte_offset(_require(data, "mode_auto", ctx), f"{ctx}.mode_auto")
    mode_manual = _byte_offset(_require(data, "mode_manual", ctx), f"{ctx}.mode_manual")
    speed_register = _byte_offset(_require(data, "speed_register", ctx), f"{ctx}.speed_register")
    speed_max = _int_field(data, "speed_max", ctx, lo=1, hi=0xFF)
    read_register = _byte_offset(_require(data, "read_register", ctx), f"{ctx}.read_register")

    # Writable registers must be in allowlist. read_register is read-only.
    _validate_writable_register(mode_register, allowlist, f"{ctx}.mode_register")
    _validate_writable_register(speed_register, allowlist, f"{ctx}.speed_register")

    return FanEC(
        mode_register=mode_register,
        mode_auto=mode_auto,
        mode_manual=mode_manual,
        speed_register=speed_register,
        speed_max=speed_max,
        read_register=read_register,
    )


def _parse_ec(data: dict) -> ECConfig:
    """Parse and validate the [ec] section."""
    ctx = "ec"
    if not isinstance(data, dict):
        raise ConfigError(f"{ctx}: expected table, got {type(data).__name__}")

    unlock_register = _byte_offset(_require(data, "unlock_register", ctx), f"{ctx}.unlock_register")
    unlock_value = _byte_offset(_require(data, "unlock_value", ctx), f"{ctx}.unlock_value")

    raw_allowlist = _require(data, "write_allowlist", ctx)
    if not isinstance(raw_allowlist, list):
        raise ConfigError(f"{ctx}.write_allowlist: expected array")
    if not raw_allowlist:
        raise ConfigError(f"{ctx}.write_allowlist: must not be empty")
    allowlist = tuple(_byte_offset(v, f"{ctx}.write_allowlist[{i}]")
                      for i, v in enumerate(raw_allowlist))

    # unlock_register is itself writable
    _validate_writable_register(unlock_register, allowlist, f"{ctx}.unlock_register")

    fans_data = data.get("fans", {})
    if not isinstance(fans_data, dict):
        raise ConfigError(f"{ctx}.fans: expected table")
    fans = {}
    for name, fan_data in fans_data.items():
        fans[name] = _parse_fan_ec(fan_data, name, allowlist)

    return ECConfig(
        unlock_register=unlock_register,
        unlock_value=unlock_value,
        write_allowlist=allowlist,
        fans=MappingProxyType(fans),
    )


def _parse_ec_rpm(data: Any) -> Mapping[str, ECRpmFan]:
    """Parse and validate the [ec_rpm] section."""
    ctx = "ec_rpm"
    if not isinstance(data, dict):
        raise ConfigError(f"{ctx}: expected table, got {type(data).__name__}")
    if not data:
        raise ConfigError(f"{ctx}: must not be empty if defined")

    fans: dict[str, ECRpmFan] = {}
    for fan_name, fan_data in data.items():
        if not isinstance(fan_name, str) or not fan_name.strip():
            raise ConfigError(f"{ctx}: fan name must be a non-empty string")
        fctx = f"{ctx}.{fan_name}"
        if not isinstance(fan_data, dict):
            raise ConfigError(f"{fctx}: expected table, got {type(fan_data).__name__}")

        low_reg = _byte_offset(_require(fan_data, "low_register", fctx), f"{fctx}.low_register")
        high_reg = _byte_offset(_require(fan_data, "high_register", fctx), f"{fctx}.high_register")
        if low_reg == high_reg:
            raise ConfigError(
                f"{fctx}: low_register and high_register must be distinct bytes, got {low_reg:#04x}"
            )

        encoding = fan_data.get("encoding", "u16_le")
        if not isinstance(encoding, str):
            raise ConfigError(f"{fctx}.encoding: expected string, got {type(encoding).__name__}")
        if encoding not in SUPPORTED_EC_RPM_ENCODINGS:
            raise ConfigError(
                f"{fctx}.encoding: '{encoding}' not supported; must be one of {list(SUPPORTED_EC_RPM_ENCODINGS)}"
            )

        fans[fan_name] = ECRpmFan(
            low_register=low_reg,
            high_register=high_reg,
            encoding=encoding,
        )

    return MappingProxyType(fans)


def parse_curve_points(raw: Any, context: str = "curve") -> tuple[CurvePoint, ...]:
    """Parse and validate a list of [temp_c, percent] pairs."""
    if not isinstance(raw, list):
        raise ConfigError(f"{context}: expected array of [temp, percent] pairs")
    if not raw:
        raise ConfigError(f"{context}: curve must have at least one point")
    points: list[CurvePoint] = []
    prev_temp: float | None = None
    for i, pair in enumerate(raw):
        pc = f"{context}[{i}]"
        if not isinstance(pair, list) or len(pair) != 2:
            raise ConfigError(f"{pc}: expected [temp_c, percent] pair")
        t, p = pair
        t = _float_field({"temp_c": t}, "temp_c", pc)
        if isinstance(p, bool) or not isinstance(p, int):
            raise ConfigError(f"{pc}: percent must be integer")
        if not 0 <= p <= 100:
            raise ConfigError(f"{pc}: percent {p} out of range 0..100")
        if prev_temp is not None and t <= prev_temp:
            raise ConfigError(
                f"{pc}: temperatures must be strictly increasing "
                f"({t} <= previous {prev_temp})"
            )
        prev_temp = t
        points.append(CurvePoint(temp_c=t, percent=p))
    return tuple(points)


_parse_curve_points = parse_curve_points


def _parse_curves(data: dict) -> CurvesConfig:
    """Parse and validate the [curves] section."""
    ctx = "curves"
    if not isinstance(data, dict):
        raise ConfigError(f"{ctx}: expected table")
    fans: dict[str, tuple[CurvePoint, ...]] = {}
    for name, raw in data.items():
        fans[name] = _parse_curve_points(raw, f"{ctx}.{name}")
    return CurvesConfig(fans=MappingProxyType(fans))


# -----------------------------------------------------------------------
# Top-level loader
# -----------------------------------------------------------------------

def load_config(path: str | Path | None = None) -> Config:
    """
    Load and validate config from a TOML file.

    Raises ConfigError on any validation failure with a descriptive message.
    Raises FileNotFoundError / OSError if the file cannot be read.
    """
    if path is None:
        path = DEFAULT_CONFIG_PATH
    path = Path(path)
    with open(path, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"{path}: {error}") from error
    return parse_config(raw)


def _parse_nbfc(data: Any) -> NBFCConfig:
    """Parse and validate the [nbfc] section."""
    ctx = "nbfc"
    if not isinstance(data, dict):
        raise ConfigError(f"{ctx}: expected table, got {type(data).__name__}")
    allowed = set(NBFCConfig.__dataclass_fields__)
    if data.keys() - allowed:
        raise ConfigError(f"nbfc: unknown fields {sorted(data.keys() - allowed)}")
    manifest_path = data.get("manifest_path", "/opt/nitrofan/build/manifest.json")
    if not isinstance(manifest_path, str) or not manifest_path.strip():
        raise ConfigError(f"{ctx}.manifest_path: expected non-empty string")
    runtime_dir = data.get("runtime_dir", "/opt/nitrofan/runtime")
    if not isinstance(runtime_dir, str) or not runtime_dir.strip():
        raise ConfigError(f"{ctx}.runtime_dir: expected non-empty string")

    optional_strs: dict[str, str | None] = {}
    for key in ("policy_path", "profile_path", "service_bin", "ec_path", "sysfs_root", "synthetic_root"):
        if key in data:
            val = data[key]
            if val is not None and (not isinstance(val, str) or not val.strip()):
                raise ConfigError(f"{ctx}.{key}: expected non-empty string")
            optional_strs[key] = val
        else:
            optional_strs[key] = None

    return NBFCConfig(
        manifest_path=manifest_path,
        runtime_dir=runtime_dir,
        **optional_strs,
    )


def parse_config(raw: dict) -> Config:
    """
    Validate a parsed TOML dict and return a Config.

    This is separated from load_config so tests can validate dicts directly.
    Raises ConfigError on any problem; no partial state is exposed.
    """
    ctx = "config"
    if not isinstance(raw, dict):
        raise ConfigError("config: expected table")
    backend = _enum_field(raw, "backend", ctx, VALID_BACKENDS)
    tick_s = _float_field(raw, "tick_s", ctx, positive=True)
    reassert_s = _float_field(raw, "reassert_s", ctx, positive=True)
    min_percent = _int_field(raw, "min_percent", ctx, lo=0, hi=100)
    emergency_c = _int_field(raw, "emergency_c", ctx, lo=1, hi=150)
    startup_mode = _enum_field(raw, "startup_mode", ctx, VALID_STARTUP_MODES)

    if backend == "nbfc":
        if min_percent < 25 or tick_s > 2 or reassert_s > 10 or emergency_c > 90:
            raise ConfigError("nbfc requires min_percent >=25, tick_s <=2, reassert_s <=10 and emergency_c <=90")
        if startup_mode != "auto":
            raise ConfigError(
                f"nbfc backend requires startup_mode 'auto', got '{startup_mode}'. "
                "Manual modes and curves cannot be persisted across restarts."
            )

    ec = None
    if "ec" in raw:
        ec = _parse_ec(raw["ec"])

    ec_rpm = None
    if "ec_rpm" in raw:
        ec_rpm = _parse_ec_rpm(raw["ec_rpm"])

    nbfc = None
    if "nbfc" in raw:
        nbfc = _parse_nbfc(raw["nbfc"])
    elif backend == "nbfc":
        nbfc = NBFCConfig()

    if backend == "ec":
        has_rpm = ec_rpm is not None and bool(ec_rpm)
        has_control = ec is not None and bool(ec.fans)
        if not (has_rpm or has_control):
            raise ConfigError(
                "ec backend requires an [ec_rpm] section with fan RPM mappings "
                "or an [ec] section with fan mappings"
            )

    curves_data = raw.get("curves", {})
    curves = _parse_curves(curves_data)
    if startup_mode == "curve" and not curves.fans:
        raise ConfigError("curve startup mode requires configured curves")

    return Config(
        backend=backend,
        tick_s=tick_s,
        reassert_s=reassert_s,
        min_percent=min_percent,
        emergency_c=emergency_c,
        startup_mode=startup_mode,
        ec=ec,
        curves=curves,
        ec_rpm=ec_rpm,
        nbfc=nbfc,
    )
