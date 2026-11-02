"""
Unit and integration tests for nitrofan.sensors -- SystemSensorProvider.

Tests sysfs coretemp discovery, neutral Acer temperature collection, awake-only
targeted NVIDIA GPU telemetry, error handling, non-recursive directory scans,
and dynamic path rediscovery on driver reload or device removal.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from nitrofan.sensors import SystemSensorProvider


class TestSystemSensorProvider(unittest.TestCase):
    """Test real temperature provider with fake sysfs tree."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.sysfs_root = self.root / "sysfs"
        self.hwmon_base = self.sysfs_root / "sys" / "class" / "hwmon"
        self.pci_base = self.sysfs_root / "sys" / "bus" / "pci" / "devices"

        self.hwmon_base.mkdir(parents=True, exist_ok=True)
        self.pci_base.mkdir(parents=True, exist_ok=True)

        # 1. Setup coretemp hwmon
        self.coretemp_dir = self.hwmon_base / "hwmon1"
        self.coretemp_dir.mkdir(parents=True, exist_ok=True)
        (self.coretemp_dir / "name").write_text("coretemp\n")
        (self.coretemp_dir / "temp1_label").write_text("Package id 0\n")
        (self.coretemp_dir / "temp1_input").write_text("51000\n")
        (self.coretemp_dir / "temp2_label").write_text("Core 0\n")
        (self.coretemp_dir / "temp2_input").write_text("48000\n")

        # 2. Setup acer hwmon
        self.acer_dir = self.hwmon_base / "hwmon5"
        self.acer_dir.mkdir(parents=True, exist_ok=True)
        (self.acer_dir / "name").write_text("acer\n")
        (self.acer_dir / "fan1_input").write_text("2400\n")
        (self.acer_dir / "fan2_input").write_text("2200\n")
        (self.acer_dir / "temp1_input").write_text("46000\n")
        (self.acer_dir / "temp2_input").write_text("44000\n")
        (self.acer_dir / "temp3_input").write_text("42000\n")

        # 3. Setup NVIDIA PCI display and audio devices
        self.gpu_dir = self.pci_base / "0000:01:00.0"
        (self.gpu_dir / "power").mkdir(parents=True, exist_ok=True)
        (self.gpu_dir / "vendor").write_text("0x10de\n")
        (self.gpu_dir / "class").write_text("0x030000\n")
        (self.gpu_dir / "power" / "runtime_status").write_text("active\n")

        # Audio function on GPU: class 0x040300
        self.gpu_audio_dir = self.pci_base / "0000:01:00.1"
        (self.gpu_audio_dir / "power").mkdir(parents=True, exist_ok=True)
        (self.gpu_audio_dir / "vendor").write_text("0x10de\n")
        (self.gpu_audio_dir / "class").write_text("0x040300\n")
        (self.gpu_audio_dir / "power" / "runtime_status").write_text("active\n")

        self.mock_runner = MagicMock()
        self.mock_runner.return_value = subprocess.CompletedProcess(
            args=["nvidia-smi"],
            returncode=0,
            stdout="55\n",
            stderr="",
        )

        self.provider = SystemSensorProvider(
            sysfs_root=self.sysfs_root,
            subprocess_runner=self.mock_runner,
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_full_successful_read(self) -> None:
        temps = self.provider()
        self.assertEqual(temps.get("cpu"), 51.0)
        self.assertEqual(temps.get("gpu"), 55.0)
        self.assertEqual(temps.get("acer_temp1"), 46.0)
        self.assertEqual(temps.get("acer_temp2"), 44.0)
        self.assertEqual(temps.get("acer_temp3"), 42.0)

        # Verify nvidia-smi invocation targeted the display PCI device
        self.mock_runner.assert_called_once()
        cmd = self.mock_runner.call_args[0][0]
        self.assertEqual(
            cmd,
            [
                "nvidia-smi",
                "--id=0000:01:00.0",
                "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
        )

    def test_nvidia_audio_function_ignored(self) -> None:
        # If display device is removed and only audio function exists:
        os.remove(self.gpu_dir / "class")
        self.provider.rediscover()
        self.mock_runner.reset_mock()
        temps = self.provider()
        self.assertIsNone(temps.get("gpu"))
        self.mock_runner.assert_not_called()

    def test_nvidia_suspended_skips_query(self) -> None:
        (self.gpu_dir / "power" / "runtime_status").write_text("suspended\n")
        self.mock_runner.reset_mock()
        temps = self.provider()
        self.assertIsNone(temps.get("gpu"))
        self.mock_runner.assert_not_called()

    def test_nvidia_smi_failure_returns_none(self) -> None:
        # Non-zero exit code
        self.mock_runner.return_value = subprocess.CompletedProcess(
            args=["nvidia-smi"],
            returncode=1,
            stdout="",
            stderr="Error: GPU fallen off bus",
        )
        temps = self.provider()
        self.assertIsNone(temps.get("gpu"))

        # TimeoutExpired
        self.mock_runner.side_effect = subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=3.0)
        temps = self.provider()
        self.assertIsNone(temps.get("gpu"))

        # FileNotFoundError (driver / command missing)
        self.mock_runner.side_effect = FileNotFoundError("nvidia-smi not found")
        temps = self.provider()
        self.assertIsNone(temps.get("gpu"))

    def test_nvidia_smi_corrupted_output_returns_none(self) -> None:
        self.mock_runner.side_effect = None
        for bad in ["N/A", "999", "-5", "abc", ""]:
            self.mock_runner.return_value = subprocess.CompletedProcess(
                args=["nvidia-smi"], returncode=0, stdout=bad, stderr=""
            )
            temps = self.provider()
            self.assertIsNone(temps.get("gpu"))

    def test_cpu_temp_fallback_without_package_label(self) -> None:
        # Remove Package id 0 label; should fall back to temp1_input
        os.remove(self.coretemp_dir / "temp1_label")
        self.provider.rediscover()
        temps = self.provider()
        self.assertEqual(temps.get("cpu"), 51.0)

    def test_cpu_read_failure_returns_none(self) -> None:
        (self.coretemp_dir / "temp1_input").write_text("corrupted\n")
        temps = self.provider()
        self.assertIsNone(temps.get("cpu"))

        (self.coretemp_dir / "temp1_input").write_text("200000\n")  # 200C out of range
        temps = self.provider()
        self.assertIsNone(temps.get("cpu"))

    def test_rediscovery_after_hwmon_renumbering(self) -> None:
        # Simulate driver unload and reload: hwmon5 -> hwmon6
        temps = self.provider()
        self.assertEqual(temps.get("acer_temp1"), 46.0)

        new_acer_dir = self.hwmon_base / "hwmon6"
        os.rename(self.acer_dir, new_acer_dir)

        # Next read discovers the new path
        temps = self.provider()
        self.assertEqual(temps.get("acer_temp1"), 46.0)

    def test_device_removal_returns_none(self) -> None:
        # Remove acer device
        import shutil
        shutil.rmtree(self.acer_dir)

        temps = self.provider()
        self.assertEqual(temps.get("cpu"), 51.0)
        self.assertEqual(temps.get("gpu"), 55.0)
        # Acer readings disappear
        self.assertNotIn("acer_temp1", temps)

    def test_reused_hwmon_numbers_do_not_report_another_device(self) -> None:
        os.rename(self.coretemp_dir, self.hwmon_base / "hwmon7")
        os.rename(self.acer_dir, self.hwmon_base / "hwmon8")
        for old_dir in (self.coretemp_dir, self.acer_dir):
            old_dir.mkdir()
            (old_dir / "name").write_text("nvme\n")
            (old_dir / "temp1_input").write_text("99000\n")
        temps = self.provider()
        self.assertEqual(temps["cpu"], 51.0)
        self.assertEqual(temps["acer_temp1"], 46.0)

    def test_gpu_identity_rechecked_before_query(self) -> None:
        (self.gpu_dir / "class").write_text("0x040300\n")
        self.assertIsNone(self.provider()["gpu"])
        self.mock_runner.assert_not_called()

    def test_oversized_temperature_returns_none_without_losing_other_sensors(self) -> None:
        (self.coretemp_dir / "temp1_input").write_text("9" * 400 + "\n")
        temps = self.provider()
        self.assertIsNone(temps["cpu"])
        self.assertEqual(temps["acer_temp1"], 46.0)


if __name__ == "__main__":
    unittest.main()
