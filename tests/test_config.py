"""Tests for nitrofan.config -- TOML loading and validation."""

import os
import tempfile
import unittest
from pathlib import Path

from nitrofan.config import (
    Config,
    ConfigError,
    CurvePoint,
    CurvesConfig,
    ECConfig,
    FanEC,
    load_config,
    parse_config,
)


# -----------------------------------------------------------------------
# A valid baseline config dict matching the spec's candidate EC layout
# -----------------------------------------------------------------------

def _base_config() -> dict:
    """Return a valid config dict that parse_config accepts."""
    return {
        "backend": "mock",
        "tick_s": 2,
        "reassert_s": 10,
        "min_percent": 25,
        "emergency_c": 90,
        "startup_mode": "auto",
        "ec": {
            "unlock_register": 0x03,
            "unlock_value": 0x11,
            "write_allowlist": [0x03, 0x21, 0x22, 0x37, 0x3A],
            "fans": {
                "cpu": {
                    "mode_register": 0x22,
                    "mode_auto": 0x04,
                    "mode_manual": 0x0C,
                    "speed_register": 0x37,
                    "speed_max": 0x64,
                    "read_register": 0x13,
                },
                "gpu": {
                    "mode_register": 0x21,
                    "mode_auto": 0x10,
                    "mode_manual": 0x30,
                    "speed_register": 0x3A,
                    "speed_max": 0x64,
                    "read_register": 0x15,
                },
            },
        },
        "curves": {
            "cpu": [[45, 25], [60, 40], [70, 60], [80, 85], [88, 100]],
            "gpu": [[45, 25], [60, 40], [70, 60], [78, 85], [85, 100]],
        },
    }


class TestValidConfig(unittest.TestCase):
    """Verify a fully valid config parses without error."""

    def test_baseline_parses(self):
        cfg = parse_config(_base_config())
        self.assertIsInstance(cfg, Config)
        self.assertEqual(cfg.backend, "mock")
        self.assertEqual(cfg.tick_s, 2.0)
        self.assertEqual(cfg.reassert_s, 10.0)
        self.assertEqual(cfg.min_percent, 25)
        self.assertEqual(cfg.emergency_c, 90)
        self.assertEqual(cfg.startup_mode, "auto")

    def test_ec_section(self):
        cfg = parse_config(_base_config())
        self.assertIsNotNone(cfg.ec)
        self.assertEqual(cfg.ec.unlock_register, 0x03)
        self.assertEqual(cfg.ec.unlock_value, 0x11)
        self.assertIn(0x22, cfg.ec.write_allowlist)
        self.assertEqual(len(cfg.ec.fans), 2)

    def test_cpu_fan_ec(self):
        cfg = parse_config(_base_config())
        cpu = cfg.ec.fans["cpu"]
        self.assertIsInstance(cpu, FanEC)
        self.assertEqual(cpu.mode_register, 0x22)
        self.assertEqual(cpu.mode_auto, 0x04)
        self.assertEqual(cpu.mode_manual, 0x0C)
        self.assertEqual(cpu.speed_register, 0x37)
        self.assertEqual(cpu.speed_max, 100)
        self.assertEqual(cpu.read_register, 0x13)

    def test_gpu_fan_ec(self):
        cfg = parse_config(_base_config())
        gpu = cfg.ec.fans["gpu"]
        self.assertEqual(gpu.mode_register, 0x21)
        self.assertEqual(gpu.speed_register, 0x3A)

    def test_curves(self):
        cfg = parse_config(_base_config())
        self.assertIn("cpu", cfg.curves.fans)
        cpu_curve = cfg.curves.fans["cpu"]
        self.assertEqual(len(cpu_curve), 5)
        self.assertEqual(cpu_curve[0], CurvePoint(45, 25))
        self.assertEqual(cpu_curve[-1], CurvePoint(88, 100))

    def test_read_register_not_in_allowlist(self):
        """read_register is read-only and must NOT be in write_allowlist."""
        cfg = parse_config(_base_config())
        self.assertNotIn(cfg.ec.fans["cpu"].read_register, cfg.ec.write_allowlist)
        self.assertNotIn(cfg.ec.fans["gpu"].read_register, cfg.ec.write_allowlist)

    def test_config_is_frozen(self):
        cfg = parse_config(_base_config())
        with self.assertRaises(AttributeError):
            cfg.backend = "ec"  # type: ignore[misc]

    def test_nested_maps_cannot_bypass_validation(self):
        cfg = parse_config(_base_config())
        with self.assertRaises(TypeError):
            cfg.ec.fans["cpu"] = cfg.ec.fans["gpu"]
        with self.assertRaises(TypeError):
            cfg.curves.fans["cpu"] = ()

    def test_raw_speed_max_can_use_full_byte_range(self):
        data = _base_config()
        data["ec"]["fans"]["cpu"]["speed_max"] = 255
        self.assertEqual(parse_config(data).ec.fans["cpu"].speed_max, 255)

    def test_no_ec_section(self):
        """ec section is optional (hwmon/mock backends don't need it)."""
        d = _base_config()
        del d["ec"]
        cfg = parse_config(d)
        self.assertIsNone(cfg.ec)

    def test_empty_curves(self):
        """No fan curves defined is valid (not required at top level)."""
        d = _base_config()
        d["curves"] = {}
        del d["ec"]
        cfg = parse_config(d)
        self.assertEqual(len(cfg.curves.fans), 0)

    def test_ec_backend_valid(self):
        d = _base_config()
        d["backend"] = "ec"
        cfg = parse_config(d)
        self.assertEqual(cfg.backend, "ec")

    def test_hwmon_backend_valid(self):
        d = _base_config()
        d["backend"] = "hwmon"
        del d["ec"]
        cfg = parse_config(d)
        self.assertEqual(cfg.backend, "hwmon")


class TestConfigValidation(unittest.TestCase):
    """Verify that bad config produces clear ConfigError."""

    def _should_fail(self, d: dict, substring: str = ""):
        with self.assertRaises(ConfigError) as cm:
            parse_config(d)
        if substring:
            self.assertIn(substring, str(cm.exception))

    # -- missing top-level keys --

    def test_missing_backend(self):
        d = _base_config()
        del d["backend"]
        self._should_fail(d, "backend")

    def test_missing_tick_s(self):
        d = _base_config()
        del d["tick_s"]
        self._should_fail(d, "tick_s")

    def test_missing_reassert_s(self):
        d = _base_config()
        del d["reassert_s"]
        self._should_fail(d, "reassert_s")

    def test_missing_min_percent(self):
        d = _base_config()
        del d["min_percent"]
        self._should_fail(d, "min_percent")

    def test_missing_emergency_c(self):
        d = _base_config()
        del d["emergency_c"]
        self._should_fail(d, "emergency_c")

    def test_missing_startup_mode(self):
        d = _base_config()
        del d["startup_mode"]
        self._should_fail(d, "startup_mode")

    # -- invalid enum values --

    def test_invalid_backend(self):
        d = _base_config()
        d["backend"] = "usb"
        self._should_fail(d, "not in")

    def test_invalid_startup_mode(self):
        d = _base_config()
        d["startup_mode"] = "turbo"
        self._should_fail(d, "not in")

    # -- type errors --

    def test_backend_not_string(self):
        d = _base_config()
        d["backend"] = 42
        self._should_fail(d, "expected string")

    def test_tick_s_boolean(self):
        d = _base_config()
        d["tick_s"] = True
        self._should_fail(d, "boolean")

    def test_min_percent_boolean(self):
        d = _base_config()
        d["min_percent"] = False
        self._should_fail(d, "boolean")

    def test_min_percent_float(self):
        d = _base_config()
        d["min_percent"] = 25.5
        self._should_fail(d, "expected integer")

    # -- range errors --

    def test_tick_s_zero(self):
        d = _base_config()
        d["tick_s"] = 0
        self._should_fail(d, "positive")

    def test_tick_s_negative(self):
        d = _base_config()
        d["tick_s"] = -1
        self._should_fail(d, "positive")

    def test_min_percent_negative(self):
        d = _base_config()
        d["min_percent"] = -1
        self._should_fail(d, "minimum")

    def test_min_percent_over_100(self):
        d = _base_config()
        d["min_percent"] = 101
        self._should_fail(d, "maximum")

    def test_emergency_c_zero(self):
        d = _base_config()
        d["emergency_c"] = 0
        self._should_fail(d, "minimum")

    # -- EC validation --

    def test_writable_register_not_in_allowlist(self):
        """A writable register outside the allowlist must be rejected."""
        d = _base_config()
        d["ec"]["fans"]["cpu"]["mode_register"] = 0xFF  # not in allowlist
        self._should_fail(d, "write_allowlist")

    def test_speed_register_not_in_allowlist(self):
        d = _base_config()
        d["ec"]["fans"]["gpu"]["speed_register"] = 0xBB
        self._should_fail(d, "write_allowlist")

    def test_unlock_register_not_in_allowlist(self):
        d = _base_config()
        d["ec"]["unlock_register"] = 0xFE
        self._should_fail(d, "write_allowlist")

    def test_register_out_of_byte_range(self):
        d = _base_config()
        d["ec"]["fans"]["cpu"]["mode_register"] = 0x100
        self._should_fail(d, "out of range")

    def test_register_negative(self):
        d = _base_config()
        d["ec"]["fans"]["cpu"]["speed_register"] = -1
        self._should_fail(d, "out of range")

    def test_speed_max_zero(self):
        d = _base_config()
        d["ec"]["fans"]["cpu"]["speed_max"] = 0
        self._should_fail(d, "minimum")

    def test_speed_max_over_byte_range(self):
        d = _base_config()
        d["ec"]["fans"]["cpu"]["speed_max"] = 256
        self._should_fail(d, "maximum")

    def test_empty_allowlist(self):
        d = _base_config()
        d["ec"]["write_allowlist"] = []
        self._should_fail(d, "must not be empty")

    def test_allowlist_not_array(self):
        d = _base_config()
        d["ec"]["write_allowlist"] = 0x03
        self._should_fail(d, "expected array")

    def test_missing_ec_unlock_register(self):
        d = _base_config()
        del d["ec"]["unlock_register"]
        self._should_fail(d, "unlock_register")

    def test_missing_fan_mode_register(self):
        d = _base_config()
        del d["ec"]["fans"]["cpu"]["mode_register"]
        self._should_fail(d, "mode_register")

    # -- Curve validation --

    def test_curve_empty(self):
        d = _base_config()
        d["curves"]["cpu"] = []
        self._should_fail(d, "at least one point")

    def test_curve_not_strictly_increasing(self):
        d = _base_config()
        d["curves"]["cpu"] = [[50, 30], [50, 40]]
        self._should_fail(d, "strictly increasing")

    def test_curve_decreasing_temps(self):
        d = _base_config()
        d["curves"]["cpu"] = [[60, 30], [50, 40]]
        self._should_fail(d, "strictly increasing")

    def test_curve_percent_over_100(self):
        d = _base_config()
        d["curves"]["cpu"] = [[50, 110]]
        self._should_fail(d, "out of range")

    def test_curve_percent_negative(self):
        d = _base_config()
        d["curves"]["cpu"] = [[50, -5]]
        self._should_fail(d, "out of range")

    def test_curve_bad_pair_length(self):
        d = _base_config()
        d["curves"]["cpu"] = [[50, 30, 99]]
        self._should_fail(d, "pair")

    def test_fractional_curve_temperatures(self):
        d = _base_config()
        d["curves"]["cpu"] = [[50.5, 30]]
        self.assertEqual(parse_config(d).curves.fans["cpu"][0].temp_c, 50.5)

    def test_ec_backend_requires_register_mappings(self):
        for missing_section in (True, False):
            with self.subTest(missing_section=missing_section):
                data = _base_config()
                data["backend"] = "ec"
                if missing_section:
                    del data["ec"]
                else:
                    data["ec"]["fans"] = {}
                self._should_fail(data, "fan mappings")

    def test_curve_startup_requires_curves(self):
        data = _base_config()
        data["startup_mode"] = "curve"
        data["curves"] = {}
        self._should_fail(data, "requires configured curves")

    def test_curve_percent_boolean(self):
        d = _base_config()
        d["curves"]["cpu"] = [[50, True]]
        self._should_fail(d, "integer")

    def test_curve_single_point_valid(self):
        d = _base_config()
        d["curves"]["cpu"] = [[50, 50]]
        cfg = parse_config(d)
        self.assertEqual(len(cfg.curves.fans["cpu"]), 1)

    def test_valid_ec_rpm_section(self):
        d = _base_config()
        d["ec_rpm"] = {
            "fan1": {"low_register": 0x13, "high_register": 0x14, "encoding": "u16_le"},
            "fan2": {"low_register": 0x15, "high_register": 0x16},
        }
        cfg = parse_config(d)
        self.assertIsNotNone(cfg.ec_rpm)
        self.assertIn("fan1", cfg.ec_rpm)
        self.assertIn("fan2", cfg.ec_rpm)
        self.assertEqual(cfg.ec_rpm["fan1"].low_register, 0x13)
        self.assertEqual(cfg.ec_rpm["fan1"].high_register, 0x14)
        self.assertEqual(cfg.ec_rpm["fan1"].encoding, "u16_le")
        self.assertEqual(cfg.ec_rpm["fan2"].encoding, "u16_le")

    def test_ec_backend_with_only_ec_rpm_accepted(self):
        d = _base_config()
        d["backend"] = "ec"
        del d["ec"]
        d["ec_rpm"] = {
            "fan1": {"low_register": 0x13, "high_register": 0x14},
        }
        cfg = parse_config(d)
        self.assertEqual(cfg.backend, "ec")
        self.assertIsNone(cfg.ec)
        self.assertIsNotNone(cfg.ec_rpm)

    def test_ec_rpm_invalid_offsets(self):
        bad_offsets = [-1, 256, True, "0x13", 1.5]
        for bad in bad_offsets:
            with self.subTest(bad=bad):
                d = _base_config()
                d["ec_rpm"] = {
                    "fan1": {"low_register": bad, "high_register": 0x14},
                }
                self._should_fail(d, "byte offset")

    def test_ec_rpm_identical_registers_rejected(self):
        d = _base_config()
        d["ec_rpm"] = {
            "fan1": {"low_register": 0x13, "high_register": 0x13},
        }
        self._should_fail(d, "distinct bytes")

    def test_ec_rpm_unsupported_encoding_rejected(self):
        d = _base_config()
        d["ec_rpm"] = {
            "fan1": {"low_register": 0x13, "high_register": 0x14, "encoding": "u16_be"},
        }
        self._should_fail(d, "not supported")

    def test_ec_rpm_empty_fan_name_rejected(self):
        d = _base_config()
        d["ec_rpm"] = {
            "": {"low_register": 0x13, "high_register": 0x14},
        }
        self._should_fail(d, "non-empty string")

    def test_ec_rpm_empty_table_rejected(self):
        d = _base_config()
        d["ec_rpm"] = {}
        self._should_fail(d, "must not be empty")

    def test_ec_rpm_arbitrary_registers_allowed(self):
        d = _base_config()
        d["ec_rpm"] = {
            "fan_custom": {"low_register": 0x80, "high_register": 0x81},
        }
        cfg = parse_config(d)
        self.assertEqual(cfg.ec_rpm["fan_custom"].low_register, 0x80)
        self.assertEqual(cfg.ec_rpm["fan_custom"].high_register, 0x81)

    def test_backward_compatible_config_positional(self):
        from nitrofan.config import Config, CurvesConfig
        cfg = Config(
            backend="mock",
            tick_s=2.0,
            reassert_s=10.0,
            min_percent=25,
            emergency_c=90,
            startup_mode="auto",
            ec=None,
            curves=CurvesConfig(fans={}),
        )
        self.assertIsNone(cfg.ec_rpm)


class TestLoadConfigFile(unittest.TestCase):
    """Test loading from an actual TOML file."""

    def test_load_valid_toml(self):
        toml_text = """\
backend = "mock"
tick_s = 2
reassert_s = 10
min_percent = 25
emergency_c = 90
startup_mode = "auto"

[ec]
unlock_register = 0x03
unlock_value = 0x11
write_allowlist = [0x03, 0x21, 0x22, 0x37, 0x3A]

[ec.fans.cpu]
mode_register = 0x22
mode_auto = 0x04
mode_manual = 0x0C
speed_register = 0x37
speed_max = 100
read_register = 0x13

[ec.fans.gpu]
mode_register = 0x21
mode_auto = 0x10
mode_manual = 0x30
speed_register = 0x3A
speed_max = 100
read_register = 0x15

[curves]
cpu = [[45, 25], [60, 40], [70, 60], [80, 85], [88, 100]]
gpu = [[45, 25], [60, 40], [70, 60], [78, 85], [85, 100]]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(toml_text)
            f.flush()
            path = f.name
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.backend, "mock")
            self.assertEqual(cfg.ec.fans["cpu"].mode_register, 0x22)
            self.assertEqual(len(cfg.curves.fans["cpu"]), 5)
        finally:
            os.unlink(path)

    def test_load_ec_readonly_example_toml(self):
        example_path = Path(__file__).resolve().parent.parent / "examples" / "ec-readonly.toml"
        cfg = load_config(example_path)
        self.assertEqual(cfg.backend, "ec")
        self.assertEqual(cfg.startup_mode, "auto")
        self.assertIsNone(cfg.ec)
        self.assertIsNotNone(cfg.ec_rpm)
        self.assertIn("fan1", cfg.ec_rpm)
        self.assertIn("fan2", cfg.ec_rpm)
        self.assertEqual(cfg.ec_rpm["fan1"].low_register, 0x13)
        self.assertEqual(cfg.ec_rpm["fan1"].high_register, 0x14)
        self.assertEqual(cfg.ec_rpm["fan2"].low_register, 0x15)
        self.assertEqual(cfg.ec_rpm["fan2"].high_register, 0x16)

    def test_load_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            load_config("/nonexistent/config.toml")

    def test_load_bad_toml_syntax(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("not valid [[[toml\n")
            path = f.name
        try:
            with self.assertRaises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
