#!/usr/bin/env python3
"""
monitor.py - Phase0 read-only hardware sensor poller for Acer Nitro AN515-54.

Reads temperatures and fan RPM from sysfs hwmon nodes. Optionally captures
microphone audio for relative loudness (RMS dBFS). Optionally queries an NVIDIA
GPU only when the PCI device is confirmed active. No writes, no module loads,
no /dev/ec access.

Output is CSV to stdout or a file; one row per sample interval.
"""

import argparse
import csv
import glob
import io
import math
import os
import re
import struct
import subprocess
import sys
import time


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FULL_SCALE_S16 = 32768.0
SILENCE_RMS_THRESHOLD = 1.0   # samples below this are treated as silence
NVIDIA_VENDOR_ID = "0x10de"
NVIDIA_CLASS_PREFIX = "0x03"  # display class


# ---------------------------------------------------------------------------
# Warning deduplication
# ---------------------------------------------------------------------------

_warned: set[str] = set()


def warn_once(msg: str) -> None:
    if msg not in _warned:
        print(f"WARNING: {msg}", file=sys.stderr)
        _warned.add(msg)


# ---------------------------------------------------------------------------
# sysfs sensor discovery
# ---------------------------------------------------------------------------

def _read_sysfs(path: str) -> str | None:
    """Return stripped file content or None on any error."""
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return None


def _hwmon_label(hwmon_dir: str, basename: str) -> str:
    """
    Return a human-readable label for a sensor file.

    Priority: <basename>_label file, then the bare basename.
    """
    label_path = os.path.join(hwmon_dir, basename + "_label")
    label = _read_sysfs(label_path)
    return label if label else basename


def discover_sensors(sysfs_root: str = "") -> list[dict]:
    """
    Walk /sys/class/hwmon and return one dict per readable sensor.

    Each dict has:
      path        - absolute path to the _input file
      col         - stable CSV column name: <chip>/<hwmon_id>/<label>
      kind        - "temp" or "fan"
      hwmon_id    - e.g. "hwmon0"
    """
    sensors: list[dict] = []
    hwmon_base = os.path.join(sysfs_root, "sys", "class", "hwmon")
    if not sysfs_root:
        hwmon_base = "/sys/class/hwmon"

    dirs = sorted(glob.glob(os.path.join(hwmon_base, "hwmon*")))
    for hwmon_dir in dirs:
        hwmon_id = os.path.basename(hwmon_dir)

        chip_raw = _read_sysfs(os.path.join(hwmon_dir, "name")) or hwmon_id

        for input_path in sorted(glob.glob(os.path.join(hwmon_dir, "temp*_input"))):
            basename = os.path.basename(input_path)[: -len("_input")]
            label = _hwmon_label(hwmon_dir, basename)
            col = f"{chip_raw}/{hwmon_id}/{label}/{basename}_c"
            sensors.append(
                {"path": input_path, "col": col, "kind": "temp", "hwmon_id": hwmon_id}
            )

        for input_path in sorted(glob.glob(os.path.join(hwmon_dir, "fan*_input"))):
            basename = os.path.basename(input_path)[: -len("_input")]
            label = _hwmon_label(hwmon_dir, basename)
            col = f"{chip_raw}/{hwmon_id}/{label}/{basename}_rpm"
            sensors.append(
                {"path": input_path, "col": col, "kind": "fan", "hwmon_id": hwmon_id}
            )

    return sensors


def read_sensor(sensor: dict) -> str:
    """
    Read a single sensor and return a formatted string value or "".

    Temperatures are in millidegrees Celsius in sysfs; divide by 1000.
    Fan speeds are in RPM directly.
    """
    raw = _read_sysfs(sensor["path"])
    if raw is None:
        warn_once(f"cannot read {sensor['path']}")
        return ""
    try:
        val = int(raw)
    except ValueError:
        warn_once(f"unexpected value in {sensor['path']}: {raw!r}")
        return ""

    if sensor["kind"] == "temp":
        return f"{val / 1000:.1f}"
    return str(val)  # fan RPM is already in RPM


# ---------------------------------------------------------------------------
# NVIDIA GPU detection (conditional, PCI-based)
# ---------------------------------------------------------------------------

def find_nvidia_pci_ids(sysfs_root: str = "", *, active_only: bool = True) -> list[str]:
    """
    Return PCI bus IDs of NVIDIA display-class devices whose runtime_status
    is 'active', unless active_only=False is used to reserve CSV columns.

    Returns domain:bus:slot.func strings suitable for nvidia-smi -i.
    """
    pci_base = "/sys/bus/pci/devices"
    if sysfs_root:
        pci_base = os.path.join(sysfs_root, "sys", "bus", "pci", "devices")

    active_ids: list[str] = []
    if not os.path.isdir(pci_base):
        return active_ids

    for dev_dir in sorted(os.listdir(pci_base)):
        dev_path = os.path.join(pci_base, dev_dir)
        vendor = _read_sysfs(os.path.join(dev_path, "vendor"))
        if vendor != NVIDIA_VENDOR_ID:
            continue
        dev_class = _read_sysfs(os.path.join(dev_path, "class"))
        if not dev_class or not dev_class.lower().startswith(NVIDIA_CLASS_PREFIX):
            continue
        status = _read_sysfs(os.path.join(dev_path, "power", "runtime_status"))
        if active_only and status != "active":
            warn_once(
                f"NVIDIA device {dev_dir} runtime_status={status!r}; skipping GPU query"
            )
            continue
        active_ids.append(dev_dir)

    return active_ids


def query_nvidia_temp(pci_id: str, sysfs_root: str = "") -> str:
    """
    Query nvidia-smi for GPU temp of a specific PCI bus ID.

    Returns Celsius string or "" on failure. Times out after 5 seconds.
    """
    if pci_id not in find_nvidia_pci_ids(sysfs_root):
        return ""
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--id={pci_id}", "--query-gpu=temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            val = result.stdout.strip()
            if val and re.match(r"^\d+$", val):
                return val
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        warn_once(f"nvidia-smi for {pci_id} failed: {detail[:160]}")
    except FileNotFoundError:
        warn_once("nvidia-smi not found; GPU temp unavailable")
    except subprocess.TimeoutExpired:
        warn_once(f"nvidia-smi timed out for {pci_id}")
    except OSError as e:
        warn_once(f"nvidia-smi error: {e}")
    return ""


# ---------------------------------------------------------------------------
# Microphone RMS dBFS
# ---------------------------------------------------------------------------

def rms_dbfs(samples: bytes) -> tuple[float | None, bool]:
    """
    Compute RMS dBFS from raw S16_LE mono audio bytes.

    Returns (dbfs_value, clipping). dbfs_value is None if silence or empty.
    Clipping is True if any sample hits +-32767.
    """
    if len(samples) < 2:
        return None, False

    n = len(samples) // 2
    unpacked = struct.unpack(f"<{n}h", samples[: n * 2])
    clipping = any(abs(s) >= 32767 for s in unpacked)

    mean_sq = sum(s * s for s in unpacked) / n
    rms = math.sqrt(mean_sq)

    if rms < SILENCE_RMS_THRESHOLD:
        return None, clipping  # silence

    dbfs = 20.0 * math.log10(rms / FULL_SCALE_S16)
    return dbfs, clipping


def capture_mic(device: str, duration_s: float = 1.0,
                warmup_s: float = 0.0) -> bytes | None:
    """
    Capture audio using arecord. Returns raw S16_LE bytes or None on failure.

    This captures into memory only; nothing is written to disk.
    """
    sample_rate = 16000
    channels = 1
    fmt = "S16_LE"
    warmup_frames = int(sample_rate * warmup_s)
    num_frames = int(sample_rate * duration_s) + warmup_frames
    try:
        result = subprocess.run(
            [
                "arecord",
                "-D", device,
                "-f", fmt,
                "-r", str(sample_rate),
                "-c", str(channels),
                "--samples", str(num_frames),
                "-q",                  # quiet: no progress messages
                "-t", "raw",
            ],
            capture_output=True,
            timeout=duration_s + warmup_s + 3.0,
        )
        if result.returncode == 0:
            return result.stdout[warmup_frames * 2:]
        warn_once(
            f"arecord failed (rc={result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip()[:120]}"
        )
    except FileNotFoundError:
        warn_once("arecord not found; install alsa-utils for mic support")
    except subprocess.TimeoutExpired:
        warn_once("arecord timed out; mic_dbfs will be blank this sample")
    except OSError as e:
        warn_once(f"arecord error: {e}")
    return None


# ---------------------------------------------------------------------------
# --list mode
# ---------------------------------------------------------------------------

def cmd_list(sysfs_root: str = "") -> None:
    """Print detected sensors and audio devices; no recording or writes."""
    sensors = discover_sensors(sysfs_root)
    print("=== sysfs hwmon sensors ===", file=sys.stderr)
    if not sensors:
        print("  (none found)", file=sys.stderr)
    for s in sensors:
        print(f"  [{s['kind']}] {s['col']}", file=sys.stderr)
        print(f"           path: {s['path']}", file=sys.stderr)

    nvidia_ids = find_nvidia_pci_ids(sysfs_root)
    print("\n=== NVIDIA PCI devices (active only) ===", file=sys.stderr)
    if nvidia_ids:
        for dev in nvidia_ids:
            print(f"  {dev}", file=sys.stderr)
    else:
        pci_base = "/sys/bus/pci/devices"
        if sysfs_root:
            pci_base = os.path.join(sysfs_root, "sys", "bus", "pci", "devices")
        # Report all NVIDIA devices and their status so user can see why none are active
        found_any = False
        if os.path.isdir(pci_base):
            for dev_dir in sorted(os.listdir(pci_base)):
                dev_path = os.path.join(pci_base, dev_dir)
                vendor = _read_sysfs(os.path.join(dev_path, "vendor"))
                if vendor != NVIDIA_VENDOR_ID:
                    continue
                found_any = True
                status = _read_sysfs(
                    os.path.join(dev_path, "power", "runtime_status")
                ) or "unknown"
                print(f"  {dev_dir}  runtime_status={status}", file=sys.stderr)
        if not found_any:
            print("  (no NVIDIA device found in PCI tree)", file=sys.stderr)

    print("\n=== audio recording devices ===", file=sys.stderr)
    # arecord -l: card/device list
    for flag, label in [("-l", "hardware devices"), ("-L", "named PCM devices")]:
        try:
            r = subprocess.run(
                ["arecord", flag],
                capture_output=True, text=True, timeout=5
            )
            print(f"\narecord {flag} ({label}):", file=sys.stderr)
            out = r.stdout.strip() or r.stderr.strip()
            if out:
                for line in out.splitlines():
                    print(f"  {line}", file=sys.stderr)
            else:
                print("  (no output)", file=sys.stderr)
        except FileNotFoundError:
            print(
                "  arecord not found; install alsa-utils to see audio devices",
                file=sys.stderr,
            )
            break
        except subprocess.TimeoutExpired:
            print("  arecord timed out", file=sys.stderr)


# ---------------------------------------------------------------------------
# CSV column building
# ---------------------------------------------------------------------------

def build_columns(
    sensors: list[dict],
    nvidia_ids: list[str],
    use_mic: bool,
) -> list[str]:
    """Return ordered list of CSV column names."""
    cols = ["utc_timestamp", "elapsed_s"]
    for s in sensors:
        cols.append(s["col"])
    for nid in nvidia_ids:
        # Use the PCI bus id as part of the column name; it's stable per boot
        safe_id = nid.replace(":", "_").replace(".", "_")
        cols.append(f"nvidia/{safe_id}/gpu_temp_c")
    if use_mic:
        cols.extend(["mic_dbfs", "mic_clipping"])
    return cols


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def run_monitor(args: argparse.Namespace) -> None:
    sensors = discover_sensors(args.sysfs_root)
    if not sensors:
        print(
            "WARNING: no sysfs hwmon sensors found; CSV will only have timestamps",
            file=sys.stderr,
        )

    print(f"INFO: found {len(sensors)} sysfs sensor(s)", file=sys.stderr)
    for s in sensors:
        print(f"  {s['col']}: {s['path']}", file=sys.stderr)

    nvidia_ids = find_nvidia_pci_ids(args.sysfs_root, active_only=False)
    if nvidia_ids:
        print(f"INFO: NVIDIA GPU(s), queried only while active: {nvidia_ids}", file=sys.stderr)
    else:
        print(
            "INFO: no NVIDIA GPU detected; GPU columns omitted",
            file=sys.stderr,
        )

    columns = build_columns(sensors, nvidia_ids, args.mic)

    csv_file: io.TextIOWrapper | None = None
    csv_out = sys.stdout
    if args.csv:
        csv_file = open(args.csv, "w", newline="")
        csv_out = csv_file

    writer = csv.DictWriter(csv_out, fieldnames=columns)
    writer.writeheader()
    csv_out.flush()

    start_mono = time.monotonic()
    sample_count = 0
    try:
        while True:
            if args.samples is not None and sample_count >= args.samples:
                break

            now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            elapsed = time.monotonic() - start_mono
            row: dict[str, str] = {
                "utc_timestamp": now_utc,
                "elapsed_s": f"{elapsed:.3f}",
            }

            for s in sensors:
                row[s["col"]] = read_sensor(s)

            for nid in nvidia_ids:
                safe_id = nid.replace(":", "_").replace(".", "_")
                col = f"nvidia/{safe_id}/gpu_temp_c"
                row[col] = query_nvidia_temp(nid, args.sysfs_root)

            if args.mic:
                audio = capture_mic(args.mic_device, duration_s=1.0,
                                    warmup_s=args.mic_warmup)
                if audio is not None:
                    dbfs_val, clipping = rms_dbfs(audio)
                    row["mic_dbfs"] = (
                        f"{dbfs_val:.2f}" if dbfs_val is not None else ""
                    )
                    row["mic_clipping"] = "1" if clipping else "0"
                else:
                    row["mic_dbfs"] = ""
                    row["mic_clipping"] = ""

            writer.writerow(row)
            csv_out.flush()
            sample_count += 1

            if args.samples is not None and sample_count >= args.samples:
                break

            sleep_target = start_mono + sample_count * args.interval
            sleep_dur = sleep_target - time.monotonic()
            if sleep_dur > 0:
                time.sleep(sleep_dur)

    finally:
        if csv_file is not None:
            csv_file.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Phase0 read-only hardware monitor. "
            "Writes CSV rows with temperatures, fan RPM, and optional mic loudness."
        )
    )
    p.add_argument(
        "--interval",
        type=float,
        default=5.0,
        metavar="SEC",
        help="seconds between samples (default: 5)",
    )
    p.add_argument(
        "--samples",
        type=int,
        default=None,
        metavar="N",
        help="stop after N samples (default: run until Ctrl+C)",
    )
    p.add_argument(
        "--csv",
        metavar="PATH",
        default=None,
        help="write CSV to this file instead of stdout",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="list detected sensors and audio devices, then exit",
    )
    p.add_argument(
        "--mic",
        action="store_true",
        help="capture mic audio each sample via arecord and compute RMS dBFS",
    )
    p.add_argument(
        "--mic-device",
        default="default",
        metavar="DEV",
        help="ALSA device for arecord (default: 'default')",
    )
    p.add_argument("--mic-warmup", type=float, default=0.0, metavar="SEC",
                   help="discard this many seconds after opening the mic (default: 0)")
    p.add_argument(
        "--sysfs-root",
        default="",
        metavar="PATH",
        help="root to prepend for sysfs paths; useful for testing with a fake tree",
    )
    args = p.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval <= 0:
        p.error("--interval must be positive")
    if args.samples is not None and args.samples <= 0:
        p.error("--samples must be positive")
    if not math.isfinite(args.mic_warmup) or args.mic_warmup < 0:
        p.error("--mic-warmup must be finite and non-negative")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.list:
        cmd_list(args.sysfs_root)
        return
    try:
        run_monitor(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
