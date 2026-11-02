#!/usr/bin/python3
"""Max/Auto fan toggle for Acer Nitro laptops on Linux Mint.

Install once, after Mint and its drivers are set up:

    sudo python3 nitro_fans.py install

Then restart. "Fans: Max / Auto" appears in the Mint menu.

The fans are switched by Acer's own firmware interface through the kernel's
acer_wmi driver, the same switch NitroSense uses on Windows. Nothing writes to
the embedded controller directly. Only firmware Auto and firmware Max are
offered; fixed low speeds are not, so this cannot stop the fans. Every boot
starts in Auto.

Kernel 6.19 or later is needed for Max. On Mint 22.x the installer adds
Ubuntu's 7.0 HWE kernel. Undo with: sudo python3 nitro_fans.py uninstall
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("NITRO_FANS_ROOT", "/"))
INSTALLED = "/usr/local/bin/nitro-fans"
MODPROBE_CONF = "etc/modprobe.d/nitro-fans.conf"
POLKIT_POLICY = "usr/share/polkit-1/actions/org.nitrofans.policy"
UDEV_RULE = "etc/udev/rules.d/90-nitro-fans.rules"
DESKTOP_ENTRY = "usr/share/applications/nitro-fans.desktop"
MIN_KERNEL = (6, 19)
HWE_PACKAGE = "linux-generic-hwe-24.04"

# acer_wmi capability bits (drivers/platform/x86/acer-wmi.c).
CAP_MAILLED, CAP_WIRELESS, CAP_BLUETOOTH, CAP_BRIGHTNESS = 0x1, 0x2, 0x4, 0x8
CAP_THREEG, CAP_SET_FUNCTION_MODE = 0x10, 0x20
CAP_PLATFORM_PROFILE, CAP_HWMON, CAP_PWM = 0x400, 0x800, 0x1000
WMID_GUID3 = "61EF69EA-865C-4BC3-A502-A0DEBA0CB531"
RFKILL_CAPS = {"acer-wireless": CAP_WIRELESS, "acer-bluetooth": CAP_BLUETOOTH,
               "acer-threeg": CAP_THREEG}
# hwmon pwmN_enable values used by acer_wmi: 0 = firmware Max (turbo), 2 = Auto.
MODES = {"max": "0", "auto": "2"}


def path(relative: str) -> Path:
    return ROOT / relative.lstrip("/")


def read(relative: str) -> str | None:
    try:
        return path(relative).read_text().strip()
    except OSError:
        return None


# ---------------------------------------------------------------- detection

def acer_hwmon() -> Path | None:
    base = path("sys/class/hwmon")
    if not base.is_dir():
        return None
    for hwmon in sorted(base.iterdir()):
        try:
            if (hwmon / "name").read_text().strip() == "acer":
                return hwmon
        except OSError:
            continue
    return None


def has_pwm() -> bool:
    hwmon = acer_hwmon()
    return bool(hwmon and (hwmon / "pwm1_enable").exists() and (hwmon / "pwm2_enable").exists())


def has_platform_profile() -> bool:
    return path("sys/devices/platform/acer-wmi/platform-profile").exists()


def kernel_version(release: str | None = None) -> tuple[int, int]:
    release = release or os.uname().release
    parts = release.split("-")[0].split(".")
    return int(parts[0]), int(parts[1])


def driver_caps() -> int:
    """Keep what acer_wmi already detects and add profiles, sensors and fan modes."""
    device = path("sys/devices/platform/acer-wmi")
    caps = CAP_PLATFORM_PROFILE | CAP_HWMON | CAP_PWM
    rfkill = device / "rfkill"
    if rfkill.is_dir():
        for entry in rfkill.iterdir():
            try:
                caps |= RFKILL_CAPS.get((entry / "name").read_text().strip(), 0)
            except OSError:
                continue
    if path("sys/class/leds/acer-wmi::mail").exists():
        caps |= CAP_MAILLED
    if path("sys/class/backlight/acer-wmi").exists():
        caps |= CAP_BRIGHTNESS
    wmi = path("sys/bus/wmi/devices")
    if wmi.is_dir() and any(d.name.upper().startswith(WMID_GUID3) for d in wmi.iterdir()):
        caps |= CAP_SET_FUNCTION_MODE
    return caps


def driver_options() -> str:
    # Unlisted models need predator_v4=1 for modes and sensors. Keep it if it is
    # already in use, so running the installer twice cannot drop it.
    in_use = read("sys/module/acer_wmi/parameters/predator_v4") == "Y"
    options = ["predator_v4=1"] if in_use or not has_platform_profile() else []
    options.append(f"force_caps={driver_caps()}")
    return "options acer_wmi " + " ".join(options)


def check_laptop() -> str:
    vendor = read("sys/class/dmi/id/sys_vendor") or ""
    product = read("sys/class/dmi/id/product_name") or ""
    if vendor != "Acer" or not product.startswith(("Nitro", "Predator")):
        raise SystemExit(f"This is for Acer Nitro/Predator laptops; this one is {vendor} {product}.")
    if not path("sys/devices/platform/acer-wmi").is_dir():
        raise SystemExit("The acer_wmi driver is not loaded on this laptop.")
    if path("etc/systemd/system/nitrofan.service").exists():
        raise SystemExit("NitroFan's EC backend is installed here; use one fan controller only.")
    return product


# ---------------------------------------------------------------- fan modes

def current_mode() -> str | None:
    hwmon = acer_hwmon()
    if not hwmon:
        return None
    try:
        values = {(hwmon / f"pwm{i}_enable").read_text().strip() for i in (1, 2)}
    except OSError:
        return None
    for name, value in MODES.items():
        if values == {value}:
            return name
    return "other"


def set_mode(mode: str) -> None:
    if mode not in MODES:
        raise SystemExit(f"Unknown mode {mode!r}; use max or auto.")
    hwmon = acer_hwmon()
    if not hwmon or not has_pwm():
        raise SystemExit("Max fans are not available. Restart after installing, or the "
                         "firmware does not support it.")
    for i in (1, 2):
        (hwmon / f"pwm{i}_enable").write_text(MODES[mode])


def rpm() -> list[int]:
    hwmon = acer_hwmon()
    speeds = []
    for i in (1, 2):
        try:
            speeds.append(int((hwmon / f"fan{i}_input").read_text().strip()))
        except (OSError, TypeError, ValueError):
            pass
    return speeds


def notify(message: str) -> None:
    print(message)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-i", "weather-windy", "Nitro fans", message], check=False)


def toggle() -> int:
    """Run from the menu as the desktop user; the change itself goes through pkexec."""
    if not has_pwm():
        notify("Max fans are not available on this laptop yet. Restart, then try again.")
        return 1
    target = "auto" if current_mode() == "max" else "max"
    result = subprocess.run(["pkexec", INSTALLED, "set", target], check=False)
    if result.returncode != 0 or current_mode() != target:
        notify("Could not change the fans. They are still on " + (current_mode() or "unknown") + ".")
        return 1
    time.sleep(4)
    speeds = " / ".join(f"{s} RPM" for s in rpm()) or "speed unknown"
    notify(("Fans on MAX" if target == "max" else "Fans back on Auto") + f" ({speeds})")
    return 0


# ---------------------------------------------------------------- install

POLICY = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <action id="org.nitrofans.set">
    <description>Switch the laptop fans between Max and Auto</description>
    <message>Switching the fans needs permission</message>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>yes</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">{INSTALLED}</annotate>
  </action>
</policyconfig>
"""

# Every time the Acer driver registers its fan controls (each boot), start in Auto.
RULE = f'ACTION=="add", SUBSYSTEM=="hwmon", ATTR{{name}}=="acer", RUN+="{INSTALLED} set auto"\n'

DESKTOP = f"""[Desktop Entry]
Type=Application
Name=Fans: Max / Auto
Comment=Switch the laptop fans between Max and Auto
Exec={INSTALLED} toggle
Icon=weather-windy
Terminal=false
Categories=System;Settings;
Keywords=fan;nitro;max;cooling;gaming;
"""


def write(relative: str, content: str, mode: int = 0o644) -> None:
    target = path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    target.chmod(mode)


def install(run=subprocess.run, release: str | None = None) -> int:
    product = check_laptop()
    print(f"Setting up Max/Auto fans for the {product}.")
    options = driver_options()
    if kernel_version(release) < MIN_KERNEL:
        print(f"Installing the newer kernel ({HWE_PACKAGE}); this takes a few minutes.")
        run(["apt-get", "install", "-y", HWE_PACKAGE], check=True)
    write(INSTALLED, Path(__file__).read_text(), 0o755)
    write(MODPROBE_CONF, "# Acer firmware fan modes (nitro_fans.py)\n" + options + "\n")
    write(POLKIT_POLICY, POLICY)
    write(UDEV_RULE, RULE)
    write(DESKTOP_ENTRY, DESKTOP)
    print(f"Driver setting: {options}")
    print("Done. Restart the laptop, then open the menu and choose \"Fans: Max / Auto\".")
    return 0


def uninstall() -> int:
    for relative in (MODPROBE_CONF, POLKIT_POLICY, UDEV_RULE, DESKTOP_ENTRY, INSTALLED):
        path(relative).unlink(missing_ok=True)
    print("Removed. Restart to return the Acer driver to its defaults.")
    return 0


def status() -> int:
    print(f"Kernel: {os.uname().release}")
    print(f"Max/Auto available: {'yes' if has_pwm() else 'no'}")
    print(f"Mode: {current_mode() or 'unknown'}")
    print(f"Fans: {', '.join(f'{s} RPM' for s in rpm()) or 'unknown'}")
    print(f"Driver setting now: {driver_options()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Acer Nitro Max/Auto fans")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install")
    sub.add_parser("uninstall")
    sub.add_parser("toggle")
    sub.add_parser("status")
    setter = sub.add_parser("set")
    setter.add_argument("mode", choices=sorted(MODES))
    args = parser.parse_args(argv)

    if args.command in ("install", "uninstall", "set") and os.geteuid() != 0 and ROOT == Path("/"):
        raise SystemExit("Run this with sudo.")
    if args.command == "install":
        return install()
    if args.command == "uninstall":
        return uninstall()
    if args.command == "set":
        if not has_pwm():
            return 1
        set_mode(args.mode)
        return 0
    if args.command == "toggle":
        return toggle()
    return status()


if __name__ == "__main__":
    sys.exit(main())
