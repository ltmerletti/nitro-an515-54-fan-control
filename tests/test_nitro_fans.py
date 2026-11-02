"""Tests for the Linux Mint Max/Auto fan toggle (nitro_fans.py)."""

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parent.parent / "nitro_fans.py"
spec = importlib.util.spec_from_file_location("nitro_fans", SOURCE)
nitro_fans = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nitro_fans)


class FakeLaptop:
    def __init__(self, root: Path, product="Nitro AN515-56", pwm=True, profile=False):
        self.root = root
        dmi = root / "sys/class/dmi/id"
        dmi.mkdir(parents=True)
        (dmi / "sys_vendor").write_text("Acer\n")
        (dmi / "product_name").write_text(product + "\n")
        dev = root / "sys/devices/platform/acer-wmi"
        for index, name in enumerate(("acer-wireless", "acer-bluetooth")):
            entry = dev / "rfkill" / f"rfkill{index}"
            entry.mkdir(parents=True)
            (entry / "name").write_text(name + "\n")
        if profile:
            (dev / "platform-profile").mkdir()
        wmi = root / "sys/bus/wmi/devices"
        wmi.mkdir(parents=True)
        (wmi / (nitro_fans.WMID_GUID3 + "-1")).mkdir()
        (wmi / "05901221-D566-11D1-B2F0-00A0C9062910").mkdir()
        other = root / "sys/class/hwmon/hwmon2"
        other.mkdir(parents=True)
        (other / "name").write_text("coretemp\n")
        self.hwmon = root / "sys/class/hwmon/hwmon5"
        self.hwmon.mkdir(parents=True)
        (self.hwmon / "name").write_text("acer\n")
        for i in (1, 2):
            (self.hwmon / f"fan{i}_input").write_text("2400\n")
            if pwm:
                (self.hwmon / f"pwm{i}_enable").write_text("2\n")

    def pwm(self):
        return [(self.hwmon / f"pwm{i}_enable").read_text().strip() for i in (1, 2)]


OLD = "6.14.0-37-generic"
NEW = "7.0.0-42-generic"


class FakeSystem:
    """Simulates apt, dpkg-query, modinfo and dkms for the kernel step."""

    def __init__(self, root: Path, nvidia=None, apt_brings_nvidia=False, prebuilt_fixes=False,
                 dkms_fixes=False, hwe_meta=False, kernel_installs=True):
        self.root, self.calls = root, []
        self.nvidia = dict(nvidia or {})   # kernel -> (version, license)
        self.apt_brings_nvidia, self.prebuilt_fixes = apt_brings_nvidia, prebuilt_fixes
        self.dkms_fixes, self.kernel_installs = dkms_fixes, kernel_installs
        self.packages = {"linux-image-" + OLD}
        if hwe_meta:
            self.packages.add("linux-generic-hwe-24.04")
        self.add_kernel(OLD)

    def add_kernel(self, release):
        d = self.root / "lib/modules" / release
        d.mkdir(parents=True, exist_ok=True)
        (d / "modules.order").write_text("")

    def result(self, code=0, out=""):
        return mock.Mock(returncode=code, stdout=out)

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[:2] == ["modinfo", "-k"]:
            info = self.nvidia.get(cmd[2])
            if not info:
                return self.result(1)
            return self.result(0, (info[0] if cmd[4] == "version" else info[1]) + "\n")
        if cmd[:2] == ["dpkg-query", "-W"] and cmd[3] == "${Status}":
            ok = cmd[4] in self.packages
            return self.result(0 if ok else 1, "install ok installed" if ok else "")
        if cmd[:2] == ["dpkg-query", "-W"]:
            pattern = cmd[4].strip("*")
            return self.result(0, "\n".join(p for p in sorted(self.packages) if pattern in p))
        if cmd == ["apt-get", "update"]:
            return self.result()
        if cmd == ["apt-get", "install", "-y", "linux-generic-hwe-24.04"]:
            if self.kernel_installs:
                self.add_kernel(NEW)
                self.packages |= {"linux-generic-hwe-24.04", "linux-image-generic-hwe-24.04",
                                  "linux-headers-generic-hwe-24.04", "linux-image-" + NEW,
                                  "linux-modules-" + NEW, "linux-hwe-7.0-headers-7.0.0-42"}
                if self.apt_brings_nvidia:
                    self.nvidia[NEW] = self.nvidia[OLD]
            return self.result()
        if cmd[:3] == ["apt-get", "install", "-y"] and cmd[3].startswith("linux-modules-nvidia-"):
            if self.prebuilt_fixes:
                self.nvidia[NEW] = self.nvidia[OLD]
                self.packages.add(cmd[3])
                return self.result()
            return self.result(100)
        if cmd[:2] == ["dkms", "autoinstall"]:
            if self.dkms_fixes:
                self.nvidia[NEW] = self.nvidia[OLD]
            return self.result()
        if cmd[:3] == ["apt-get", "remove", "-y"]:
            self.removed = cmd[3:]
            self.packages -= set(cmd[3:])
            if "linux-image-" + NEW in cmd:
                import shutil
                shutil.rmtree(self.root / "lib/modules" / NEW)
            return self.result()
        raise AssertionError(f"unexpected command {cmd}")


class TestNitroFans(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(nitro_fans, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_caps_keep_detected_features_and_add_fan_modes(self):
        FakeLaptop(self.root)
        caps = nitro_fans.driver_caps()
        self.assertEqual(caps, 0x2 | 0x4 | 0x20 | 0x400 | 0x800 | 0x1000)
        self.assertEqual(nitro_fans.driver_options(),
                         f"options acer_wmi predator_v4=1 force_caps={caps}")

    def test_listed_models_do_not_need_predator_v4(self):
        FakeLaptop(self.root, profile=True)
        self.assertNotIn("predator_v4", nitro_fans.driver_options())

    def test_reinstall_keeps_predator_v4_once_profiles_exist(self):
        FakeLaptop(self.root, profile=True)
        param = self.root / "sys/module/acer_wmi/parameters/predator_v4"
        param.parent.mkdir(parents=True)
        param.write_text("Y\n")
        self.assertIn("predator_v4=1", nitro_fans.driver_options())

    def test_brightness_and_mail_led_are_kept_when_present(self):
        FakeLaptop(self.root)
        (self.root / "sys/class/backlight/acer-wmi").mkdir(parents=True)
        (self.root / "sys/class/leds/acer-wmi::mail").mkdir(parents=True)
        caps = nitro_fans.driver_caps()
        self.assertTrue(caps & nitro_fans.CAP_BRIGHTNESS and caps & nitro_fans.CAP_MAILLED)

    def test_only_max_and_auto_can_be_set(self):
        laptop = FakeLaptop(self.root)
        self.assertEqual(nitro_fans.set_mode("max"), 0)
        self.assertEqual(laptop.pwm(), ["0", "0"])
        self.assertEqual(nitro_fans.current_mode(), "max")
        self.assertEqual(nitro_fans.set_mode("auto"), 0)
        self.assertEqual(laptop.pwm(), ["2", "2"])
        with self.assertRaises(SystemExit):
            nitro_fans.set_mode("custom")
        with self.assertRaises(SystemExit):
            nitro_fans.main(["set", "1"])
        self.assertEqual(laptop.pwm(), ["2", "2"])

    def test_refused_max_is_reported_and_both_fans_return_to_auto(self):
        laptop = FakeLaptop(self.root)
        real_write = Path.write_text

        def refuse_second_fan(self_path, data, *args, **kwargs):
            if self_path.name == "pwm2_enable" and data == "0":
                raise OSError(5, "Input/output error")
            return real_write(self_path, data, *args, **kwargs)

        with mock.patch.object(Path, "write_text", refuse_second_fan), \
             redirect_stderr(io.StringIO()) as err:
            self.assertEqual(nitro_fans.main(["set", "max"]), nitro_fans.REFUSED)
        self.assertEqual(laptop.pwm(), ["2", "2"])
        self.assertIn("refused max", err.getvalue())

    def test_ignored_switch_is_reported(self):
        FakeLaptop(self.root)
        with mock.patch.object(nitro_fans, "current_mode", return_value="auto"), \
             redirect_stderr(io.StringIO()) as err:
            self.assertEqual(nitro_fans.set_mode("max"), nitro_fans.REFUSED)
        self.assertIn("did not switch", err.getvalue())

    def test_set_without_fan_modes_changes_nothing(self):
        FakeLaptop(self.root, pwm=False)
        self.assertEqual(nitro_fans.main(["set", "max"]), 1)
        self.assertFalse((self.root / "sys/class/hwmon/hwmon5/pwm1_enable").exists())

    def test_panel_entry_goes_beside_the_battery_once(self):
        default = ["panel1:left:0:menu@cinnamon.org", "panel1:right:8:sound@cinnamon.org",
                   "panel1:right:9:power@cinnamon.org:12", "panel1:right:10:calendar@cinnamon.org"]
        added = nitro_fans.panel_with_applet(default)
        self.assertEqual(added[-1], "panel1:right:9:" + nitro_fans.APPLET_UUID)
        self.assertEqual(added[:-1], default)
        self.assertIsNone(nitro_fans.panel_with_applet(added))
        self.assertEqual(nitro_fans.panel_without_applet(added), default)
        self.assertEqual(nitro_fans.panel_with_applet([])[-1], "panel1:right:0:" + nitro_fans.APPLET_UUID)

    def test_gsettings_values_parse(self):
        self.assertEqual(nitro_fans.parse_strv("@as []\n"), [])
        self.assertEqual(nitro_fans.parse_strv("['a:b:c:d', 'e']\n"), ["a:b:c:d", "e"])
        with self.assertRaises(ValueError):
            nitro_fans.parse_strv("[1, 2]")

    def test_panel_update_runs_gsettings_as_the_desktop_user(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="['panel1:right:9:power@cinnamon.org']\n")

        with mock.patch.dict(nitro_fans.os.environ, {"SUDO_USER": "friend"}), \
             mock.patch.object(nitro_fans.pwd, "getpwnam", return_value=mock.Mock(pw_uid=1000)):
            self.assertTrue(nitro_fans.update_panel(nitro_fans.panel_with_applet, fake_run))
        self.assertEqual(calls[0][:5], ["sudo", "-u", "friend", "env",
                                        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"])
        self.assertEqual(calls[1][5:9], ["gsettings", "set", "org.cinnamon", "enabled-applets"])
        self.assertEqual(nitro_fans.parse_strv(calls[1][9]),
                         ["panel1:right:9:power@cinnamon.org", "panel1:right:9:" + nitro_fans.APPLET_UUID])

    def test_panel_update_skipped_without_a_desktop_user(self):
        with mock.patch.dict(nitro_fans.os.environ, {}, clear=True):
            self.assertFalse(nitro_fans.update_panel(nitro_fans.panel_with_applet, lambda *a, **k: None))

    def test_applet_only_offers_auto_and_max_through_the_helper(self):
        js = nitro_fans.APPLET_JS
        self.assertIn('const HELPER = "/usr/local/bin/nitro-fans";', js)
        self.assertIn('argv: ["pkexec", HELPER, "set", mode]', js)
        self.assertEqual(js.count('_setMode("'), 2)
        self.assertIn("const REFUSED = 2;", js)
        self.assertIn("Max isn't supported on this laptop", js)
        self.assertNotIn("writeFile", js)
        self.assertIn(nitro_fans.APPLET_UUID, nitro_fans.APPLET_METADATA)

    def install_with(self, system, release=OLD):
        out = io.StringIO()
        with redirect_stdout(out):
            nitro_fans.install(run=system, release=release)
        return out.getvalue()

    def assert_nothing_written(self):
        for relative in (nitro_fans.MODPROBE_CONF, nitro_fans.POLKIT_POLICY, nitro_fans.UDEV_RULE,
                         nitro_fans.APPLET_DIR, nitro_fans.INSTALLED):
            self.assertFalse((self.root / relative.lstrip("/")).exists(), relative)

    def test_install_on_old_kernel_adds_hwe_and_writes_files(self):
        FakeLaptop(self.root)
        system = FakeSystem(self.root)
        output = self.install_with(system)
        self.assertIn(["apt-get", "install", "-y", "linux-generic-hwe-24.04"], system.calls)
        self.assertIn("Restart the laptop", output)
        conf = (self.root / nitro_fans.MODPROBE_CONF).read_text()
        self.assertIn("force_caps=", conf)
        rule = (self.root / nitro_fans.UDEV_RULE).read_text()
        self.assertIn('ATTR{name}=="acer"', rule)
        self.assertIn("set auto", rule)
        policy = (self.root / nitro_fans.POLKIT_POLICY).read_text()
        self.assertIn(f">{nitro_fans.INSTALLED}<", policy)
        self.assertIn("<allow_active>yes</allow_active>", policy)
        applet = self.root / nitro_fans.APPLET_DIR
        self.assertTrue((applet / "metadata.json").is_file())
        self.assertEqual((applet / "applet.js").read_text(), nitro_fans.APPLET_JS)
        helper = self.root / nitro_fans.INSTALLED.lstrip("/")
        self.assertEqual(helper.read_text(), SOURCE.read_text())
        self.assertTrue(helper.stat().st_mode & 0o111)

    def test_nvidia_ready_for_new_kernel_needs_no_repair(self):
        FakeLaptop(self.root)
        system = FakeSystem(self.root, nvidia={OLD: ("580.95.05", "NVIDIA")}, apt_brings_nvidia=True)
        self.install_with(system)
        self.assertFalse(any(c[:2] == ["dkms", "autoinstall"] for c in system.calls))
        self.assertTrue((self.root / nitro_fans.MODPROBE_CONF).exists())

    def test_missing_nvidia_is_repaired_with_the_prebuilt_package(self):
        FakeLaptop(self.root)
        system = FakeSystem(self.root, nvidia={OLD: ("580.95.05", "Dual MIT/GPL")}, prebuilt_fixes=True)
        self.install_with(system)
        self.assertIn(["apt-get", "install", "-y", "linux-modules-nvidia-580-open-generic-hwe-24.04"],
                      system.calls)
        self.assertTrue((self.root / "lib/modules" / NEW).is_dir())
        self.assertTrue((self.root / nitro_fans.MODPROBE_CONF).exists())

    def test_missing_nvidia_is_repaired_with_dkms(self):
        FakeLaptop(self.root)
        system = FakeSystem(self.root, nvidia={OLD: ("550.120", "NVIDIA")}, dkms_fixes=True)
        self.install_with(system)
        self.assertIn(["apt-get", "install", "-y", "linux-modules-nvidia-550-generic-hwe-24.04"],
                      system.calls)
        self.assertIn(["dkms", "autoinstall", "-k", NEW], system.calls)
        self.assertTrue((self.root / "lib/modules" / NEW).is_dir())

    def test_unfixable_nvidia_removes_the_new_kernel_and_changes_nothing(self):
        FakeLaptop(self.root)
        system = FakeSystem(self.root, nvidia={OLD: ("580.95.05", "NVIDIA")})
        with self.assertRaises(SystemExit) as stop, redirect_stdout(io.StringIO()):
            nitro_fans.install(run=system, release=OLD)
        self.assertIn("start exactly as before", str(stop.exception))
        self.assertFalse((self.root / "lib/modules" / NEW).exists())
        self.assertTrue((self.root / "lib/modules" / OLD).exists())
        for package in ("linux-image-" + NEW, "linux-generic-hwe-24.04", "linux-image-generic-hwe-24.04"):
            self.assertIn(package, system.removed)
        self.assertNotIn("linux-image-" + OLD, system.removed)
        self.assertIn("linux-image-" + OLD, system.packages)
        self.assert_nothing_written()

    def test_hwe_track_is_left_to_mint_updates(self):
        FakeLaptop(self.root)
        system = FakeSystem(self.root, nvidia={OLD: ("580.95.05", "NVIDIA")}, hwe_meta=True)
        output = self.install_with(system)
        self.assertFalse(any(c[:2] in (["apt-get", "install"], ["apt-get", "remove"]) for c in system.calls))
        self.assertIn("Update Manager", output)
        self.assertTrue((self.root / nitro_fans.MODPROBE_CONF).exists())

    def test_new_kernel_already_installed_needs_no_apt(self):
        FakeLaptop(self.root)
        system = FakeSystem(self.root)
        system.add_kernel(NEW)
        output = self.install_with(system)
        self.assertFalse(any(c[0] == "apt-get" for c in system.calls))
        self.assertIn("Restart the laptop", output)

    def test_failed_kernel_install_changes_nothing(self):
        FakeLaptop(self.root)
        system = FakeSystem(self.root, kernel_installs=False)
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            nitro_fans.install(run=system, release=OLD)
        self.assert_nothing_written()

    def test_kernel_ordering(self):
        self.assertLess(nitro_fans.kernel_sort_key("6.17.0-14-generic"),
                        nitro_fans.kernel_sort_key("7.0.0-9-generic"))
        self.assertLess(nitro_fans.kernel_sort_key("7.0.0-9-generic"),
                        nitro_fans.kernel_sort_key("7.0.0-42-generic"))

    def test_install_on_new_kernel_skips_apt(self):
        FakeLaptop(self.root)
        calls = []
        with redirect_stdout(io.StringIO()):
            nitro_fans.install(run=lambda cmd, **kw: calls.append(cmd), release="7.0.0-31-generic")
        self.assertEqual(calls, [])

    def test_uninstall_removes_everything(self):
        FakeLaptop(self.root)
        with redirect_stdout(io.StringIO()):
            nitro_fans.install(run=lambda cmd, **kw: None, release="7.0.0-31-generic")
            nitro_fans.uninstall(run=lambda cmd, **kw: None)
        for relative in (nitro_fans.MODPROBE_CONF, nitro_fans.POLKIT_POLICY, nitro_fans.UDEV_RULE,
                         nitro_fans.APPLET_DIR, nitro_fans.INSTALLED):
            self.assertFalse((self.root / relative.lstrip("/")).exists(), relative)

    def test_refuses_other_laptops_and_nitrofan_hosts(self):
        FakeLaptop(self.root, product="Aspire A515-56")
        with self.assertRaises(SystemExit):
            nitro_fans.check_laptop()
        (self.root / "sys/class/dmi/id/product_name").write_text("Nitro AN515-54\n")
        unit = self.root / "etc/systemd/system/nitrofan.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("")
        with self.assertRaises(SystemExit):
            nitro_fans.check_laptop()

    def test_kernel_version_parsing(self):
        self.assertEqual(nitro_fans.kernel_version("6.17.0-14-generic"), (6, 17))
        self.assertGreaterEqual(nitro_fans.kernel_version("7.0.0-31-generic"), nitro_fans.MIN_KERNEL)


if __name__ == "__main__":
    unittest.main()
