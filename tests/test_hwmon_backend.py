"""
Unit tests for nitrofan.backends.hwmon -- HwmonBackend.

Tests discovery by name 'acer', fan IDs fan1/fan2 from actual inputs,
read_rpm validation, read_percent returning None, explicit rejection of set_percent,
idempotent open/close, path rediscovery across driver reloads, and absolute zero
write guarantee.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from nitrofan.backends.hwmon import HwmonBackend


class TestHwmonBackend(unittest.TestCase):
    """Test HwmonBackend read-only operations and safety constraints."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.hwmon_base = self.root / "sys" / "class" / "hwmon"
        self.hwmon_base.mkdir(parents=True, exist_ok=True)

        # Setup an unrelated hwmon device to ensure name matching works
        other_dir = self.hwmon_base / "hwmon0"
        other_dir.mkdir(parents=True, exist_ok=True)
        (other_dir / "name").write_text("acpitz\n")
        (other_dir / "temp1_input").write_text("30000\n")

        # Setup acer hwmon device
        self.acer_dir = self.hwmon_base / "hwmon5"
        self.acer_dir.mkdir(parents=True, exist_ok=True)
        (self.acer_dir / "name").write_text("acer\n")
        (self.acer_dir / "fan1_input").write_text("2450\n")
        (self.acer_dir / "fan2_input").write_text("2300\n")
        (self.acer_dir / "temp1_input").write_text("45000\n")

        self.backend = HwmonBackend(sysfs_root=self.root)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_capabilities_and_initial_state(self) -> None:
        self.assertEqual(self.backend.name, "hwmon")
        self.assertTrue(self.backend.read_only)
        self.assertFalse(self.backend.can_control)
        self.assertFalse(self.backend.is_open)

    def test_open_discovers_acer_and_fans(self) -> None:
        self.backend.open()
        self.assertTrue(self.backend.is_open)
        self.assertEqual(self.backend.fans, ["fan1", "fan2"])

    def test_missing_device_raises_descriptive_error(self) -> None:
        # Point to empty hwmon root
        empty_root = self.root / "empty"
        empty_hwmon = empty_root / "sys" / "class" / "hwmon"
        empty_hwmon.mkdir(parents=True, exist_ok=True)
        backend = HwmonBackend(sysfs_root=empty_root)
        with self.assertRaises(RuntimeError) as ctx:
            backend.open()
        self.assertIn("acer hwmon device not found", str(ctx.exception))
        self.assertIn("predator_v4=1", str(ctx.exception))

    def test_missing_hwmon_dir_raises_file_not_found(self) -> None:
        backend = HwmonBackend(sysfs_root=self.root / "nonexistent")
        with self.assertRaises(FileNotFoundError):
            backend.open()

    def test_read_rpm(self) -> None:
        self.backend.open()
        self.assertEqual(self.backend.read_rpm("fan1"), 2450)
        self.assertEqual(self.backend.read_rpm("fan2"), 2300)

    def test_read_rpm_before_open_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self.backend.read_rpm("fan1")

    def test_read_rpm_invalid_fan_raises(self) -> None:
        self.backend.open()
        with self.assertRaises(ValueError):
            self.backend.read_rpm("fan3")

    def test_read_rpm_corrupted_or_negative_returns_none(self) -> None:
        self.backend.open()
        (self.acer_dir / "fan1_input").write_text("invalid\n")
        self.assertIsNone(self.backend.read_rpm("fan1"))

        (self.acer_dir / "fan1_input").write_text("-100\n")
        self.assertIsNone(self.backend.read_rpm("fan1"))

    def test_read_percent_always_returns_none(self) -> None:
        self.backend.open()
        # RPM is not percent; hwmon exposes no PWM percent
        self.assertIsNone(self.backend.read_percent("fan1"))
        self.assertIsNone(self.backend.read_percent("fan2"))
        with self.assertRaises(ValueError):
            self.backend.read_percent("unknown_fan")

    def test_set_percent_explicitly_rejected(self) -> None:
        self.backend.open()
        with self.assertRaises(PermissionError) as ctx:
            self.backend.set_percent("fan1", 50)
        self.assertIn("read-only", str(ctx.exception))
        self.assertIn("control is not supported", str(ctx.exception))

    def test_set_auto_does_no_writes(self) -> None:
        self.backend.open()
        # Should execute silently without error or file writes
        self.backend.set_auto()

    def test_close_is_idempotent(self) -> None:
        self.backend.open()
        self.assertTrue(self.backend.is_open)
        self.backend.close()
        self.assertFalse(self.backend.is_open)
        self.backend.close()
        self.assertFalse(self.backend.is_open)

    def test_rediscovery_on_hwmon_renumbering(self) -> None:
        self.backend.open()
        self.assertEqual(self.backend.read_rpm("fan1"), 2450)

        # Move hwmon5 to hwmon7 (driver reloaded)
        new_dir = self.hwmon_base / "hwmon7"
        os.rename(self.acer_dir, new_dir)

        # Reading fan1 safely rediscovers and reads new path
        self.assertEqual(self.backend.read_rpm("fan1"), 2450)

    def test_zero_hardware_writes_guarantee(self) -> None:
        # Snapshot mtimes and file contents
        self.backend.open()
        mtimes_before = {
            p: (p.stat().st_mtime, p.read_bytes())
            for p in self.acer_dir.iterdir()
        }

        # Perform all operations
        self.backend.read_rpm("fan1")
        self.backend.read_rpm("fan2")
        self.backend.read_percent("fan1")
        self.backend.set_auto()
        with self.assertRaises(PermissionError):
            self.backend.set_percent("fan1", 100)
        self.backend.close()

        # Verify nothing was written or altered
        for p, (mtime, content) in mtimes_before.items():
            self.assertEqual(p.stat().st_mtime, mtime)
            self.assertEqual(p.read_bytes(), content)

    def test_reused_hwmon_number_does_not_report_another_device(self) -> None:
        self.backend.open()
        os.rename(self.acer_dir, self.hwmon_base / "hwmon7")
        self.acer_dir.mkdir()
        (self.acer_dir / "name").write_text("other_device\n")
        (self.acer_dir / "fan1_input").write_text("9999\n")
        self.assertEqual(self.backend.read_rpm("fan1"), 2450)


if __name__ == "__main__":
    unittest.main()
