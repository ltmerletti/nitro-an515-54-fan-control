#!/usr/bin/env python3
"""
ec_discover.py - Phase 0 read-only EC discovery collector for Acer Nitro AN515-54.

Collects raw ACPI Embedded Controller (EC) register readings, correlated with
real CPU/GPU temperatures and neutral fan1/fan2 tachometer RPM from acer_wmi.

Strictly read-only: zero hardware writes, zero control sequence attempts,
no unlock sequences, no battery-bit changes, and no write-support module loading.

Streams structured JSON lines to stdout, flushed after each sample.
Diagnostics and errors are emitted to stderr.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure nitrofan package is importable without requiring pip install or manual PYTHONPATH
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nitrofan.backends.hwmon import HwmonBackend
from nitrofan.config import Config, ConfigError, load_config
from nitrofan.ec import ECReader
from nitrofan.sensors import SystemSensorProvider


def resolve_default_config_path() -> Path | None:
    """Resolve default config file from repository examples or /etc/nitrofan."""
    example_path = REPO_ROOT / "examples" / "config.toml"
    if example_path.is_file():
        return example_path
    etc_path = Path("/etc/nitrofan/config.toml")
    if etc_path.is_file():
        return etc_path
    return None


def get_candidate_registers(cfg: Config) -> list[int]:
    """
    Extract candidate EC register offsets from configuration.

    Includes unlock_register plus mode_register, speed_register, and read_register
    for all configured fans in cfg.ec, and low/high registers for all fans in cfg.ec_rpm.
    Returns a sorted, deduplicated list of offsets.
    Raises ValueError if config lacks both [ec] and [ec_rpm] fan mappings.
    """
    has_legacy = cfg.ec is not None and bool(cfg.ec.fans)
    has_rpm = cfg.ec_rpm is not None and bool(cfg.ec_rpm)

    if not (has_legacy or has_rpm):
        raise ValueError(
            "Configuration file lacks a valid [ec] section or [ec_rpm] section with configured fans. "
            "An EC register mapping is required to identify candidate registers."
        )

    registers: set[int] = set()
    if cfg.ec is not None and cfg.ec.fans:
        registers.add(cfg.ec.unlock_register)
        for fan in cfg.ec.fans.values():
            registers.add(fan.mode_register)
            registers.add(fan.speed_register)
            registers.add(fan.read_register)

    if cfg.ec_rpm is not None:
        for fan_rpm in cfg.ec_rpm.values():
            registers.add(fan_rpm.low_register)
            registers.add(fan_rpm.high_register)

    return sorted(registers)


def collect_telemetry(
    sensor_provider: SystemSensorProvider,
    hwmon_backend: HwmonBackend | None,
    hwmon_err: str | None = None,
) -> dict[str, Any]:
    """
    Collect real temperatures and fan tachometer RPM.

    Missing telemetry values are reported as None with diagnostics, never fake zero.
    """
    diagnostics: dict[str, str] = {}

    # Read tachometers next to the EC snapshot, before a GPU query can delay us.
    fans_rpm: dict[str, int | None] = {}
    if hwmon_backend is not None and hwmon_backend.is_open:
        for fan in hwmon_backend.fans:
            try:
                fans_rpm[fan] = hwmon_backend.read_rpm(fan)
            except Exception as err:
                fans_rpm[fan] = None
                diagnostics[f"hwmon_{fan}"] = f"Failed reading RPM: {err}"
            if fans_rpm[fan] is None and f"hwmon_{fan}" not in diagnostics:
                diagnostics[f"hwmon_{fan}"] = "Fan tachometer unavailable"
    else:
        fans_rpm = {"fan1": None, "fan2": None}
        if hwmon_err:
            diagnostics["hwmon"] = hwmon_err
        else:
            diagnostics["hwmon"] = "hwmon backend unavailable"

    try:
        temps = sensor_provider()
    except Exception as err:
        temps = {"cpu": None, "gpu": None}
        diagnostics["temperatures"] = f"Temperature polling error: {err}"

    if temps.get("cpu") is None and "temperatures" not in diagnostics:
        diagnostics["cpu_temperature"] = "coretemp CPU temperature unavailable"
    if temps.get("gpu") is None and "temperatures" not in diagnostics:
        diagnostics["gpu_temperature"] = "NVIDIA GPU temperature unavailable (device suspended or nvidia-smi failed)"

    return {
        "temperatures_c": temps,
        "fans_rpm": fans_rpm,
        "diagnostics": diagnostics,
    }


def build_sample_record(
    timestamp: str,
    elapsed_s: float,
    label: str,
    ec_path: str,
    raw_bytes: dict[int, int],
    cfg: Config,
    telemetry: dict[str, Any],
) -> dict[str, Any]:
    """
    Assemble the complete sample record.

    Candidate registers are labeled with candidate roles explicitly marked UNVERIFIED.
    Readback values are raw integer bytes, NOT scaled or presented as confirmed fan percent or RPM.
    """
    hex_registers = {f"0x{off:02x}": val for off, val in sorted(raw_bytes.items())}

    candidate_fans: dict[str, Any] = {}
    if cfg.ec and cfg.ec.fans:
        for fan_name, fan_cfg in cfg.ec.fans.items():
            candidate_fans[fan_name] = {
                "mode_register": f"0x{fan_cfg.mode_register:02x}",
                "mode_raw_value": raw_bytes.get(fan_cfg.mode_register),
                "speed_register": f"0x{fan_cfg.speed_register:02x}",
                "speed_raw_value": raw_bytes.get(fan_cfg.speed_register),
                "readback_register": f"0x{fan_cfg.read_register:02x}",
                "readback_raw_value": raw_bytes.get(fan_cfg.read_register),
                "status": "UNVERIFIED",
                "verified": False,
            }

    candidate_unlock: dict[str, Any] = {}
    if cfg.ec:
        candidate_unlock = {
            "register": f"0x{cfg.ec.unlock_register:02x}",
            "raw_value": raw_bytes.get(cfg.ec.unlock_register),
            "status": "UNVERIFIED",
            "verified": False,
        }

    rpm_fans: dict[str, Any] = {}
    if cfg.ec_rpm:
        for fan_name, rpm_cfg in cfg.ec_rpm.items():
            low_val = raw_bytes.get(rpm_cfg.low_register)
            high_val = raw_bytes.get(rpm_cfg.high_register)
            rpm_fans[fan_name] = {
                "low_register": f"0x{rpm_cfg.low_register:02x}",
                "low_raw_value": low_val,
                "high_register": f"0x{rpm_cfg.high_register:02x}",
                "high_raw_value": high_val,
                "encoding": rpm_cfg.encoding,
            }

    record: dict[str, Any] = {
        "timestamp": timestamp,
        "elapsed_s": elapsed_s,
        "label": label,
        "ec_path": ec_path,
        "registers": hex_registers,
        "candidate_roles_unverified": {
            "unlock": candidate_unlock,
            "fans": candidate_fans,
        },
        "telemetry": telemetry,
    }
    if rpm_fans:
        record["ec_rpm_configured"] = rpm_fans

    return record


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 0 read-only EC discovery collector for Acer Nitro AN515-54."
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help="Number of samples to collect (default: 1, must be >= 1)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Sampling interval in seconds (default: 2.0, must be positive finite)",
    )
    parser.add_argument(
        "--label",
        type=str,
        default="idle",
        help="Label for the collection phase (e.g. 'idle', 'load', 'return-idle')",
    )
    parser.add_argument(
        "--ec-path",
        type=str,
        default=None,
        help="Custom path to EC interface (for fixtures and tests)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to TOML configuration file (default: examples/config.toml)",
    )
    parser.add_argument(
        "--full-dump",
        action="store_true",
        help="Read all 256 bytes (0x00..0xFF) instead of candidate registers only",
    )
    parser.add_argument(
        "--sysfs-root",
        type=str,
        default="",
        help="Root directory prepended to sysfs paths (for synthetic testing)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Validate CLI arguments
    if args.samples < 1:
        print(f"ERROR: --samples must be >= 1, got {args.samples}", file=sys.stderr)
        return 1

    if not math.isfinite(args.interval) or args.interval <= 0:
        print(
            f"ERROR: --interval must be a positive finite number, got {args.interval}",
            file=sys.stderr,
        )
        return 1

    # Resolve and load configuration
    config_path: Path | None
    if args.config:
        config_path = Path(args.config)
        if not config_path.is_file():
            print(f"ERROR: Configuration file '{config_path}' not found.", file=sys.stderr)
            return 1
    else:
        config_path = resolve_default_config_path()
        if config_path is None:
            print(
                "ERROR: Configuration file not found. Provide --config PATH or ensure "
                f"'{REPO_ROOT / 'examples' / 'config.toml'}' exists.",
                file=sys.stderr,
            )
            return 1

    try:
        cfg = load_config(config_path)
    except (ConfigError, OSError) as err:
        print(f"ERROR: Failed loading configuration from '{config_path}': {err}", file=sys.stderr)
        return 1

    try:
        candidate_registers = get_candidate_registers(cfg)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    # Initialize sensors and hwmon backend for real telemetry
    sensor_provider = SystemSensorProvider(sysfs_root=args.sysfs_root)
    hwmon_backend: HwmonBackend | None = None
    hwmon_err: str | None = None

    try:
        hwmon = HwmonBackend(sysfs_root=args.sysfs_root)
        hwmon.open()
        hwmon_backend = hwmon
    except Exception as err:
        hwmon_err = f"Failed opening hwmon backend: {err}"

    ec_reader: ECReader | None = None
    start_mono = time.monotonic()

    try:
        ec_reader = ECReader(ec_path=args.ec_path)
        ec_reader.open()

        for sample_idx in range(args.samples):
            if sample_idx > 0:
                time.sleep(args.interval)

            sample_started = time.monotonic()
            elapsed_s = round(sample_started - start_mono, 4)
            timestamp = datetime.now(timezone.utc).isoformat()

            # Read EC registers. If read fails, fail nonzero immediately without emitting partial snapshot.
            if args.full_dump:
                raw_bytes = ec_reader.read_all()
            else:
                raw_bytes = ec_reader.read_registers(candidate_registers)

            telemetry = collect_telemetry(sensor_provider, hwmon_backend, hwmon_err)

            sample = build_sample_record(
                timestamp=timestamp,
                elapsed_s=elapsed_s,
                label=args.label,
                ec_path=ec_reader.path or "",
                raw_bytes=raw_bytes,
                cfg=cfg,
                telemetry=telemetry,
            )
            sample["sample_duration_s"] = round(time.monotonic() - sample_started, 4)

            sys.stdout.write(json.dumps(sample) + "\n")
            sys.stdout.flush()

    except KeyboardInterrupt:
        # Exit cleanly on Ctrl+C without stack trace
        return 130
    except (FileNotFoundError, PermissionError, RuntimeError, OSError, ValueError) as err:
        print(f"ERROR: EC collection failed: {err}", file=sys.stderr)
        return 1
    finally:
        if ec_reader is not None:
            ec_reader.close()
        if hwmon_backend is not None:
            hwmon_backend.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
