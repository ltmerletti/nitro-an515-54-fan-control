#!/usr/bin/env python3
"""Read-only check of whether an Acer Nitro uses the AN515-54 fan layout.

Copy this single file to a Linux machine (an Ubuntu live USB is enough) and run:

    sudo python3 probe_model.py > nitro-probe.json

It never writes to the embedded controller (EC). If the ec_sys driver is not
loaded, it loads it with write_support=0. It then samples the EC and fan
sensors while idle, under a short CPU load and while cooling. The load lets the
firmware ramp the fans so the fan readings can be matched to real changes.

The report answers three questions:
1. Identity: vendor, model, BIOS and EC firmware.
2. Fan readings: do the 16-bit words at 0x13 and 0x15 behave like fan RPM?
3. Control bytes: in firmware Auto, do they hold the same values as the -54?

Matching all three is strong evidence for the -54 layout. It does not prove
that fan writes work; that still needs one guarded write test on the machine.
Standard library only, Python 3.8+.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

EC_SIZE = 256
TACH_WORDS = {"fan1": 0x13, "fan2": 0x15}
# Firmware Auto values recorded on the AN515-54 (BIOS V1.33).
AN515_54_AUTO = {0x10: 0, 0x21: 0, 0x22: 0, 0x37: 0, 0x38: 0, 0x3A: 0, 0x3B: 0}
# 0x03 is reported but never compared: on the -57/-58 it also holds battery bits.
REPORTED_ONLY = (0x03,)
RPM_MAX = 8000
RPM_SPINNING = 300
HWMON_TOLERANCE = 0.10
DMI_FIELDS = ("sys_vendor", "product_name", "product_version", "board_name",
              "bios_version", "bios_date", "ec_firmware_release")


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return None


def read_identity(root: Path) -> dict[str, str | None]:
    dmi = root / "sys/class/dmi/id"
    return {field: read_text(dmi / field) for field in DMI_FIELDS}


def ensure_ec_reader(ec_path: Path, real_system: bool) -> str:
    """Make the EC readable without enabling writes. Returns the write_support state."""
    param = Path("/sys/module/ec_sys/parameters/write_support")
    if not ec_path.exists() and real_system:
        subprocess.run(["modprobe", "ec_sys", "write_support=0"], check=True)
    if not ec_path.exists():
        raise RuntimeError(f"EC interface not found at {ec_path}")
    return (read_text(param) or "unknown") if real_system else "N"


def read_ec(ec_path: Path) -> bytes:
    # O_RDONLY only. This script never opens the EC for writing.
    fd = os.open(ec_path, os.O_RDONLY)
    try:
        data = os.pread(fd, EC_SIZE, 0)
    finally:
        os.close(fd)
    if len(data) != EC_SIZE:
        raise RuntimeError(f"Short EC read: {len(data)} bytes")
    return data


def find_hwmon_fans(root: Path) -> dict[str, Path]:
    """Return acer_wmi fan tachometer inputs, if the kernel exposes them."""
    fans: dict[str, Path] = {}
    base = root / "sys/class/hwmon"
    if not base.is_dir():
        return fans
    for hwmon in sorted(base.iterdir()):
        if read_text(hwmon / "name") != "acer":
            continue
        for index in (1, 2):
            path = hwmon / f"fan{index}_input"
            if path.exists():
                fans[f"fan{index}"] = path
    return fans


def find_cpu_temp(root: Path) -> Path | None:
    base = root / "sys/class/hwmon"
    if not base.is_dir():
        return None
    for hwmon in sorted(base.iterdir()):
        if read_text(hwmon / "name") == "coretemp" and (hwmon / "temp1_input").exists():
            return hwmon / "temp1_input"
    return None


def read_int(path: Path | None) -> int | None:
    value = read_text(path) if path else None
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def word(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


def sample(ec_path: Path, hwmon_fans: dict[str, Path], cpu_temp: Path | None, phase: str) -> dict:
    data = read_ec(ec_path)
    millideg = read_int(cpu_temp)
    return {
        "t": round(time.monotonic(), 2),
        "phase": phase,
        "ec_hex": data.hex(),
        "ec_tach_rpm": {fan: word(data, offset) for fan, offset in TACH_WORDS.items()},
        "hwmon_rpm": {fan: read_int(path) for fan, path in hwmon_fans.items()},
        "cpu_c": millideg / 1000 if millideg is not None else None,
    }


def _burn(stop_at: float) -> None:
    while time.monotonic() < stop_at:
        pass


def collect(ec_path: Path, root: Path, idle_s: float, load_s: float, cool_s: float,
            interval_s: float) -> list[dict]:
    hwmon_fans = find_hwmon_fans(root)
    cpu_temp = find_cpu_temp(root)
    samples: list[dict] = []

    def run_phase(phase: str, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            samples.append(sample(ec_path, hwmon_fans, cpu_temp, phase))
            if time.monotonic() + interval_s > end:
                break
            time.sleep(interval_s)

    run_phase("idle", idle_s)
    workers = []
    if load_s > 0:
        stop_at = time.monotonic() + load_s
        workers = [multiprocessing.Process(target=_burn, args=(stop_at,), daemon=True)
                   for _ in range(os.cpu_count() or 1)]
        for worker in workers:
            worker.start()
    try:
        run_phase("load", load_s)
    finally:
        for worker in workers:
            worker.terminate()
            worker.join(timeout=2)
    run_phase("cool", cool_s)
    return samples


def analyse(samples: list[dict]) -> dict:
    checks: dict[str, dict] = {}

    tach: dict[str, dict] = {}
    for fan in TACH_WORDS:
        values = [s["ec_tach_rpm"][fan] for s in samples]
        hwmon = [(s["ec_tach_rpm"][fan], s["hwmon_rpm"].get(fan)) for s in samples
                 if s["hwmon_rpm"].get(fan) is not None]
        agree = None
        if hwmon:
            agree = all(abs(ec - hw) <= max(100, HWMON_TOLERANCE * hw) for ec, hw in hwmon)
        plausible = all(v == 0 or RPM_SPINNING <= v <= RPM_MAX for v in values)
        changed = max(values) - min(values) >= 200
        tach[fan] = {
            "register": f"0x{TACH_WORDS[fan]:02x}",
            "min_rpm": min(values), "max_rpm": max(values),
            "plausible": plausible, "changed_with_load": changed,
            "matches_hwmon": agree,
            "ok": plausible and max(values) >= RPM_SPINNING and (agree if agree is not None else changed),
        }
    checks["fan_readings"] = {"ok": all(f["ok"] for f in tach.values()), "fans": tach}

    first = bytes.fromhex(samples[0]["ec_hex"])
    control = {}
    for offset, expected in AN515_54_AUTO.items():
        seen = sorted({bytes.fromhex(s["ec_hex"])[offset] for s in samples})
        control[f"0x{offset:02x}"] = {"expected": expected, "seen": seen, "ok": seen == [expected]}
    checks["control_bytes"] = {"ok": all(c["ok"] for c in control.values()), "registers": control}
    checks["reported_only"] = {f"0x{o:02x}": first[o] for o in REPORTED_ONLY}

    if checks["fan_readings"]["ok"] and checks["control_bytes"]["ok"]:
        verdict = "matches_an515_54_layout"
        meaning = ("Fan readings and Auto control bytes match the AN515-54. "
                   "One guarded write test on this machine is still needed before control.")
    elif checks["fan_readings"]["ok"]:
        verdict = "fan_readings_match_control_differs"
        meaning = "Fan readings match, but control bytes differ in Auto. Review before any write test."
    else:
        verdict = "different_layout"
        meaning = "Fan readings were not found at the AN515-54 addresses. Do not use the -54 profile."
    return {"verdict": verdict, "meaning": meaning, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--idle", type=float, default=20, help="idle seconds (default 20)")
    parser.add_argument("--load", type=float, default=30, help="CPU load seconds (default 30, 0 to skip)")
    parser.add_argument("--cool", type=float, default=30, help="cooling seconds (default 30)")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    parser.add_argument("--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)
    parser.add_argument("--ec-path", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    real_system = args.root == Path("/") and args.ec_path is None
    ec_path = args.ec_path or Path("/sys/kernel/debug/ec/ec0/io")
    if real_system and os.geteuid() != 0:
        parser.error("run with sudo; reading the EC needs root")

    identity = read_identity(args.root)
    print(f"Model: {identity['sys_vendor']} {identity['product_name']}, BIOS {identity['bios_version']}",
          file=sys.stderr)
    write_support = ensure_ec_reader(ec_path, real_system)
    total = args.idle + args.load + args.cool
    print(f"Sampling for about {total:.0f} s (idle, CPU load, cooling). No EC writes.", file=sys.stderr)
    samples = collect(ec_path, args.root, args.idle, args.load, args.cool, args.interval)
    report = {"tool": "nitrofan probe_model", "version": 1, "identity": identity,
              "ec_write_support": write_support, **analyse(samples), "samples": samples}
    json.dump(report, sys.stdout, indent=1)
    sys.stdout.write("\n")
    print(f"Result: {report['verdict']}. {report['meaning']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
