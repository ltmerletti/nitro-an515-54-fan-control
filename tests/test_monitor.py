"""Read-only monitor tests using fake sysfs and audio samples."""
import math
import os
import struct
import sys
import unittest
import csv
import io
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import monitor
from monitor import (
    FULL_SCALE_S16, NVIDIA_VENDOR_ID, _warned, capture_mic,
    discover_sensors, find_nvidia_pci_ids, read_sensor, rms_dbfs,
)


class TestRmsDbfs(unittest.TestCase):
    def _make_samples(self, values: list[int]) -> bytes:
        return struct.pack(f"<{len(values)}h", *values)

    def test_full_scale_sine_near_minus3(self):
        # A full-scale square wave (alternating +32767 / -32767) has RMS = 32767,
        # so dBFS should be ~0. A sine at full scale is 0 dBFS peak but ~-3 dBFS RMS.
        # Use a DC offset at 32767 / sqrt(2) ~ 23170 for -3 dBFS.
        val = int(FULL_SCALE_S16 / math.sqrt(2))
        samples = self._make_samples([val] * 1000)
        dbfs, _ = rms_dbfs(samples)
        self.assertIsNotNone(dbfs)
        self.assertAlmostEqual(dbfs, -3.01, delta=0.1)

    def test_full_scale_square_wave_near_zero_dbfs(self):
        samples = self._make_samples([32767, -32767] * 500)
        dbfs, clipping = rms_dbfs(samples)
        self.assertIsNotNone(dbfs)
        self.assertAlmostEqual(dbfs, 0.0, delta=0.01)
        self.assertTrue(clipping)

    def test_silence_returns_none(self):
        samples = self._make_samples([0] * 100)
        dbfs, clipping = rms_dbfs(samples)
        self.assertIsNone(dbfs)
        self.assertFalse(clipping)

    def test_empty_bytes_returns_none(self):
        dbfs, clipping = rms_dbfs(b"")
        self.assertIsNone(dbfs)
        self.assertFalse(clipping)

    def test_clipping_detection(self):
        samples = self._make_samples([32767])
        _, clipping = rms_dbfs(samples)
        self.assertTrue(clipping)

    def test_no_clipping_below_threshold(self):
        samples = self._make_samples([1000])
        _, clipping = rms_dbfs(samples)
        self.assertFalse(clipping)

    def test_half_scale_is_minus6dbfs(self):
        val = int(FULL_SCALE_S16 / 2)
        samples = self._make_samples([val] * 1000)
        dbfs, _ = rms_dbfs(samples)
        self.assertAlmostEqual(dbfs, -6.02, delta=0.1)


class TestSysfsDiscovery(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_hwmon(self, hwmon_id: str, chip: str, sensors: dict) -> str:
        """Create a fake hwmon directory. sensors: {basename: (value, label)}"""
        hwmon_path = os.path.join(
            self.tmpdir, "sys", "class", "hwmon", hwmon_id
        )
        os.makedirs(hwmon_path, exist_ok=True)
        Path(os.path.join(hwmon_path, "name")).write_text(chip)
        for basename, (value, label) in sensors.items():
            Path(os.path.join(hwmon_path, basename + "_input")).write_text(
                str(value)
            )
            if label:
                Path(os.path.join(hwmon_path, basename + "_label")).write_text(
                    label
                )
        return hwmon_path

    def test_temp_millidegree_conversion(self):
        self._make_hwmon(
            "hwmon0", "coretemp", {"temp1": (45000, "Core 0")}
        )
        sensors = discover_sensors(self.tmpdir)
        self.assertEqual(len(sensors), 1)
        s = sensors[0]
        self.assertEqual(s["kind"], "temp")
        self.assertIn("coretemp", s["col"])
        self.assertIn("Core 0", s["col"])
        val = read_sensor(s)
        self.assertEqual(val, "45.0")

    def test_fan_rpm_no_conversion(self):
        self._make_hwmon("hwmon0", "ec_fan", {"fan1": (2400, "CPU Fan")})
        sensors = discover_sensors(self.tmpdir)
        self.assertEqual(len(sensors), 1)
        s = sensors[0]
        self.assertEqual(s["kind"], "fan")
        self.assertEqual(read_sensor(s), "2400")

    def test_missing_input_file_returns_blank(self):
        self._make_hwmon("hwmon0", "coretemp", {"temp1": (50000, "")})
        sensors = discover_sensors(self.tmpdir)
        self.assertEqual(len(sensors), 1)
        # Remove the file to simulate a gone sensor
        os.remove(sensors[0]["path"])
        val = read_sensor(sensors[0])
        self.assertEqual(val, "")

    def test_no_sensors(self):
        sensors = discover_sensors(self.tmpdir)
        self.assertEqual(sensors, [])

    def test_multiple_hwmon_nodes(self):
        self._make_hwmon("hwmon0", "acpitz", {"temp1": (30000, "acpitz")})
        self._make_hwmon(
            "hwmon1", "coretemp", {"temp1": (40000, "Core 0"), "temp2": (41000, "Core 1")}
        )
        sensors = discover_sensors(self.tmpdir)
        self.assertEqual(len(sensors), 3)
        cols = [s["col"] for s in sensors]
        self.assertTrue(any("acpitz" in c for c in cols))
        self.assertTrue(any("Core 0" in c for c in cols))

    def test_col_contains_hwmon_id(self):
        self._make_hwmon("hwmon3", "nvme", {"temp1": (35000, "Composite")})
        sensors = discover_sensors(self.tmpdir)
        self.assertEqual(len(sensors), 1)
        self.assertIn("hwmon3", sensors[0]["col"])


class TestNvidiaPciSkip(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_pci_dev(self, dev_id: str, vendor: str, dev_class: str, runtime_status: str):
        dev_path = os.path.join(
            self.tmpdir, "sys", "bus", "pci", "devices", dev_id
        )
        power_path = os.path.join(dev_path, "power")
        os.makedirs(power_path, exist_ok=True)
        Path(os.path.join(dev_path, "vendor")).write_text(vendor)
        Path(os.path.join(dev_path, "class")).write_text(dev_class)
        Path(os.path.join(power_path, "runtime_status")).write_text(runtime_status)

    def test_suspended_nvidia_not_included(self):
        self._make_pci_dev(
            "0000:01:00.0", NVIDIA_VENDOR_ID, "0x030200", "suspended"
        )
        ids = find_nvidia_pci_ids(self.tmpdir)
        self.assertEqual(ids, [])

    def test_active_nvidia_included(self):
        self._make_pci_dev(
            "0000:01:00.0", NVIDIA_VENDOR_ID, "0x030200", "active"
        )
        ids = find_nvidia_pci_ids(self.tmpdir)
        self.assertEqual(ids, ["0000:01:00.0"])

    def test_non_nvidia_vendor_skipped(self):
        self._make_pci_dev(
            "0000:00:02.0", "0x8086", "0x030000", "active"
        )
        ids = find_nvidia_pci_ids(self.tmpdir)
        self.assertEqual(ids, [])

    def test_non_display_class_skipped(self):
        self._make_pci_dev(
            "0000:01:00.1", NVIDIA_VENDOR_ID, "0x040300", "active"
        )
        ids = find_nvidia_pci_ids(self.tmpdir)
        self.assertEqual(ids, [])

    def test_missing_status_file_not_included(self):
        dev_path = os.path.join(
            self.tmpdir, "sys", "bus", "pci", "devices", "0000:01:00.0"
        )
        os.makedirs(dev_path, exist_ok=True)
        Path(os.path.join(dev_path, "vendor")).write_text(NVIDIA_VENDOR_ID)
        Path(os.path.join(dev_path, "class")).write_text("0x030200")
        # no power/runtime_status file
        ids = find_nvidia_pci_ids(self.tmpdir)
        self.assertEqual(ids, [])

    def test_gpu_sleep_and_wake_rechecked_before_each_query(self):
        device = "0000:01:00.0"
        self._make_pci_dev(device, NVIDIA_VENDOR_ID, "0x030000", "active")
        status = Path(self.tmpdir, "sys/bus/pci/devices", device,
                      "power/runtime_status")
        with patch.object(monitor.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "43\n"
            run.return_value.stderr = ""
            self.assertEqual(monitor.query_nvidia_temp(device, self.tmpdir), "43")
            status.write_text("suspended")
            self.assertEqual(monitor.query_nvidia_temp(device, self.tmpdir), "")
            self.assertEqual(run.call_count, 1)
            status.write_text("active")
            self.assertEqual(monitor.query_nvidia_temp(device, self.tmpdir), "43")
            self.assertEqual(run.call_count, 2)
            self.assertIn(f"--id={device}", run.call_args.args[0])

    def test_suspended_start_keeps_gpu_column_without_query(self):
        self._make_pci_dev("0000:01:00.0", NVIDIA_VENDOR_ID, "0x030000", "suspended")
        output = Path(self.tmpdir, "log.csv")
        args = monitor.parse_args(["--sysfs-root", self.tmpdir, "--samples", "1",
                                   "--csv", str(output)])
        with patch.object(monitor.subprocess, "run") as run, redirect_stderr(io.StringIO()):
            monitor.run_monitor(args)
        run.assert_not_called()
        with output.open() as stream:
            row = next(csv.DictReader(stream))
        column = next(name for name in row if name.endswith("gpu_temp_c"))
        self.assertEqual(row[column], "")


class TestMicCaptureFakeArecord(unittest.TestCase):
    """Test mic capture handles arecord failure gracefully."""

    def test_arecord_not_found_returns_none(self):
        # Patch PATH to exclude arecord
        orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = "/nonexistent"
        try:
            result = capture_mic("default", duration_s=0.1)
            self.assertIsNone(result)
        finally:
            os.environ["PATH"] = orig_path
            _warned.discard("arecord not found; install alsa-utils for mic support")


if __name__ == "__main__":
    unittest.main(verbosity=2)
