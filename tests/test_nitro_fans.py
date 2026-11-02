"""Tests for the Linux Mint Max/Auto fan toggle (nitro_fans.py)."""

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
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
        nitro_fans.set_mode("max")
        self.assertEqual(laptop.pwm(), ["0", "0"])
        self.assertEqual(nitro_fans.current_mode(), "max")
        nitro_fans.set_mode("auto")
        self.assertEqual(laptop.pwm(), ["2", "2"])
        with self.assertRaises(SystemExit):
            nitro_fans.set_mode("custom")
        with self.assertRaises(SystemExit):
            nitro_fans.main(["set", "1"])
        self.assertEqual(laptop.pwm(), ["2", "2"])

    def test_set_without_fan_modes_changes_nothing(self):
        FakeLaptop(self.root, pwm=False)
        self.assertEqual(nitro_fans.main(["set", "max"]), 1)
        self.assertFalse((self.root / "sys/class/hwmon/hwmon5/pwm1_enable").exists())

    def test_toggle_uses_pkexec_and_reports_rpm(self):
        laptop = FakeLaptop(self.root)

        def fake_pkexec(cmd, check):
            self.assertEqual(cmd[:2], ["pkexec", nitro_fans.INSTALLED])
            nitro_fans.set_mode(cmd[3])
            return mock.Mock(returncode=0)

        with mock.patch.object(nitro_fans.subprocess, "run", side_effect=fake_pkexec), \
             mock.patch.object(nitro_fans.shutil, "which", return_value=None), \
             mock.patch.object(nitro_fans.time, "sleep"), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nitro_fans.toggle(), 0)
            self.assertEqual(laptop.pwm(), ["0", "0"])
            self.assertEqual(nitro_fans.toggle(), 0)
        self.assertEqual(laptop.pwm(), ["2", "2"])
        self.assertIn("Fans on MAX (2400 RPM / 2400 RPM)", out.getvalue())
        self.assertIn("Fans back on Auto", out.getvalue())

    def test_toggle_without_support_explains(self):
        FakeLaptop(self.root, pwm=False)
        with mock.patch.object(nitro_fans.shutil, "which", return_value=None), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nitro_fans.toggle(), 1)
        self.assertIn("not available", out.getvalue())

    def test_install_on_old_kernel_adds_hwe_and_writes_files(self):
        FakeLaptop(self.root)
        calls = []
        with redirect_stdout(io.StringIO()):
            nitro_fans.install(run=lambda cmd, check: calls.append(cmd), release="6.17.0-14-generic")
        self.assertEqual(calls, [["apt-get", "install", "-y", "linux-generic-hwe-24.04"]])
        conf = (self.root / nitro_fans.MODPROBE_CONF).read_text()
        self.assertIn("force_caps=", conf)
        rule = (self.root / nitro_fans.UDEV_RULE).read_text()
        self.assertIn('ATTR{name}=="acer"', rule)
        self.assertIn("set auto", rule)
        policy = (self.root / nitro_fans.POLKIT_POLICY).read_text()
        self.assertIn(f">{nitro_fans.INSTALLED}<", policy)
        self.assertIn("<allow_active>yes</allow_active>", policy)
        self.assertIn("Exec=/usr/local/bin/nitro-fans toggle",
                      (self.root / nitro_fans.DESKTOP_ENTRY).read_text())
        helper = self.root / nitro_fans.INSTALLED.lstrip("/")
        self.assertEqual(helper.read_text(), SOURCE.read_text())
        self.assertTrue(helper.stat().st_mode & 0o111)

    def test_install_on_new_kernel_skips_apt(self):
        FakeLaptop(self.root)
        calls = []
        with redirect_stdout(io.StringIO()):
            nitro_fans.install(run=lambda cmd, check: calls.append(cmd), release="7.0.0-31-generic")
        self.assertEqual(calls, [])

    def test_uninstall_removes_everything(self):
        FakeLaptop(self.root)
        with redirect_stdout(io.StringIO()):
            nitro_fans.install(run=lambda cmd, check: None, release="7.0.0-31-generic")
            nitro_fans.uninstall()
        for relative in (nitro_fans.MODPROBE_CONF, nitro_fans.POLKIT_POLICY, nitro_fans.UDEV_RULE,
                         nitro_fans.DESKTOP_ENTRY, nitro_fans.INSTALLED):
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
