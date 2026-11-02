#!/usr/bin/python3
"""Max/Auto fan toggle for Acer Nitro laptops on Linux Mint.

Install once, after Mint and its drivers are set up (one line in Terminal):

    wget -qO /tmp/nitro_fans.py https://raw.githubusercontent.com/ltmerletti/nitro-an515-54-fan-control/main/nitro_fans.py && sudo python3 /tmp/nitro_fans.py install

Then restart. A fan button sits in the panel next to the clock: click it and
choose Auto or Max.

The fans are switched by Acer's own firmware interface through the kernel's
acer_wmi driver, the same switch NitroSense uses on Windows. Nothing writes to
the embedded controller directly. Only firmware Auto and firmware Max are
offered; fixed low speeds are not, so this cannot stop the fans. Every boot
starts in Auto.

Kernel 6.19 or later is needed for Max. On Mint 22.x the installer adds
Ubuntu's 7.0 HWE kernel. Undo with: sudo nitro-fans uninstall
"""

from __future__ import annotations

import argparse
import ast
import os
import pwd
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("NITRO_FANS_ROOT", "/"))
INSTALLED = "/usr/local/bin/nitro-fans"
MODPROBE_CONF = "etc/modprobe.d/nitro-fans.conf"
POLKIT_POLICY = "usr/share/polkit-1/actions/org.nitrofans.policy"
UDEV_RULE = "etc/udev/rules.d/90-nitro-fans.rules"
APPLET_UUID = "nitro-fans@nitrofans"
APPLET_DIR = "usr/share/cinnamon/applets/" + APPLET_UUID
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

APPLET_METADATA = f"""{{
  "uuid": "{APPLET_UUID}",
  "name": "Nitro Fans",
  "description": "Switch the laptop fans between Auto and Max",
  "icon": "weather-windy-symbolic",
  "max-instances": 1
}}
"""

# Cinnamon panel applet. It reads the fan state straight from sysfs every few
# seconds and changes it through pkexec, which the polkit policy allows for the
# logged-in user without a password.
APPLET_JS = """const Applet = imports.ui.applet;
const PopupMenu = imports.ui.popupMenu;
const Util = imports.misc.util;
const GLib = imports.gi.GLib;
const Mainloop = imports.mainloop;
const ByteArray = imports.byteArray;

const HELPER = "%(helper)s";

function readFile(path) {
    try {
        let [ok, contents] = GLib.file_get_contents(path);
        return ok ? ByteArray.toString(contents).trim() : null;
    } catch (e) {
        return null;
    }
}

function acerHwmon() {
    for (let i = 0; i < 64; i++) {
        let dir = "/sys/class/hwmon/hwmon" + i;
        if (readFile(dir + "/name") === "acer")
            return dir;
    }
    return null;
}

class NitroFansApplet extends Applet.TextIconApplet {
    constructor(metadata, orientation, panelHeight, instanceId) {
        super(orientation, panelHeight, instanceId);
        this.set_applet_icon_symbolic_name("weather-windy-symbolic");

        this.menuManager = new PopupMenu.PopupMenuManager(this);
        this.menu = new Applet.AppletPopupMenu(this, orientation);
        this.menuManager.addMenu(this.menu);

        this.autoItem = new PopupMenu.PopupMenuItem("Auto");
        this.autoItem.connect("activate", () => this._setMode("auto"));
        this.menu.addMenuItem(this.autoItem);
        this.maxItem = new PopupMenu.PopupMenuItem("Max");
        this.maxItem.connect("activate", () => this._setMode("max"));
        this.menu.addMenuItem(this.maxItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.infoItem = new PopupMenu.PopupMenuItem("", { reactive: false });
        this.menu.addMenuItem(this.infoItem);

        this._refresh();
        this._timer = Mainloop.timeout_add_seconds(3, () => {
            this._refresh();
            return true;
        });
    }

    on_applet_clicked() {
        this._refresh();
        this.menu.toggle();
    }

    on_applet_removed_from_panel() {
        if (this._timer)
            Mainloop.source_remove(this._timer);
        this._timer = 0;
    }

    _setMode(mode) {
        Util.spawn(["pkexec", HELPER, "set", mode]);
        Mainloop.timeout_add(800, () => {
            this._refresh();
            return false;
        });
    }

    _refresh() {
        let hwmon = acerHwmon();
        let one = hwmon ? readFile(hwmon + "/pwm1_enable") : null;
        let two = hwmon ? readFile(hwmon + "/pwm2_enable") : null;
        if (one === null || two === null) {
            this.set_applet_label("");
            this.autoItem.setSensitive(false);
            this.maxItem.setSensitive(false);
            this.infoItem.label.set_text("Max fans need a restart to start working");
            this.set_applet_tooltip("Nitro fans: not available yet");
            return;
        }
        let max = one === "0" && two === "0";
        let auto = one === "2" && two === "2";
        this.autoItem.setSensitive(true);
        this.maxItem.setSensitive(true);
        this.autoItem.setShowDot(auto);
        this.maxItem.setShowDot(max);
        this.set_applet_label(max ? "MAX" : (auto ? "Auto" : "?"));
        let speeds = [1, 2].map(i => readFile(hwmon + "/fan" + i + "_input"))
                           .filter(v => v !== null).map(v => v + " RPM").join(" / ");
        this.infoItem.label.set_text("Fans: " + (speeds || "unknown"));
        this.set_applet_tooltip("Nitro fans: " + (max ? "Max" : "Auto") + (speeds ? " (" + speeds + ")" : ""));
    }
}

function main(metadata, orientation, panelHeight, instanceId) {
    return new NitroFansApplet(metadata, orientation, panelHeight, instanceId);
}
""" % {"helper": INSTALLED}


# ---------------------------------------------------------------- panel

def parse_strv(text: str) -> list[str]:
    text = text.strip()
    if text.startswith("@as"):
        text = text[3:].strip()
    value = ast.literal_eval(text)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"unexpected gsettings value: {text!r}")
    return value


def panel_with_applet(entries: list[str]) -> list[str] | None:
    """Return the panel list with the fan applet added beside the battery, or None."""
    if any(entry.split(":")[3:4] == [APPLET_UUID] for entry in entries):
        return None
    panel, order = "panel1", 0
    for entry in entries:
        parts = entry.split(":")
        if len(parts) >= 4 and parts[3] == "power@cinnamon.org":
            panel, order = parts[0], int(parts[2])
            break
    return entries + [f"{panel}:right:{order}:{APPLET_UUID}"]


def panel_without_applet(entries: list[str]) -> list[str]:
    return [entry for entry in entries if entry.split(":")[3:4] != [APPLET_UUID]]


def as_desktop_user(command: list[str]) -> list[str] | None:
    user = os.environ.get("SUDO_USER")
    if not user or user == "root":
        return None
    uid = pwd.getpwnam(user).pw_uid
    return ["sudo", "-u", user, "env", f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
            *command]


def update_panel(change, run=subprocess.run) -> bool:
    get = as_desktop_user(["gsettings", "get", "org.cinnamon", "enabled-applets"])
    if get is None:
        return False
    current = run(get, capture_output=True, text=True, check=False)
    if current.returncode != 0:
        return False
    updated = change(parse_strv(current.stdout))
    if updated is None:
        return True
    result = run(as_desktop_user(["gsettings", "set", "org.cinnamon", "enabled-applets", repr(updated)]),
                 check=False)
    return result.returncode == 0


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
        run(["apt-get", "update"], check=True)
        run(["apt-get", "install", "-y", HWE_PACKAGE], check=True)
    write(INSTALLED, Path(__file__).read_text(), 0o755)
    write(MODPROBE_CONF, "# Acer firmware fan modes (nitro_fans.py)\n" + options + "\n")
    write(POLKIT_POLICY, POLICY)
    write(UDEV_RULE, RULE)
    write(APPLET_DIR + "/metadata.json", APPLET_METADATA)
    write(APPLET_DIR + "/applet.js", APPLET_JS)
    print(f"Driver setting: {options}")
    if update_panel(panel_with_applet, run):
        print("Added the fan button to the panel.")
    else:
        print("Add the button yourself: right-click the panel, choose Applets, and add Nitro Fans.")
    print("Done. Restart the laptop. Then click the fan button in the panel to choose Auto or Max.")
    return 0


def uninstall(run=subprocess.run) -> int:
    update_panel(panel_without_applet, run)
    for relative in (MODPROBE_CONF, POLKIT_POLICY, UDEV_RULE, APPLET_DIR + "/metadata.json",
                     APPLET_DIR + "/applet.js", INSTALLED):
        path(relative).unlink(missing_ok=True)
    try:
        path(APPLET_DIR).rmdir()
    except OSError:
        pass
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
    return status()


if __name__ == "__main__":
    sys.exit(main())
