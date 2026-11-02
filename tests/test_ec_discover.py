"""
Unit tests for tools.ec_discover -- Phase 0 EC discovery collector.

Tests candidate register extraction from arbitrary config (no hardcoded sibling offsets),
CLI argument validation, full dump vs candidate-only dump, JSON-lines output structure,
changing fixture values across samples, fatal failure on EC read errors (no partial snapshot),
clean Ctrl+C exit, and safe None telemetry with diagnostics (no fake zeros).
"""

from __future__ import annotations

import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, patch

from nitrofan.config import Config, CurvesConfig, ECConfig, FanEC
from tools.ec_discover import (
    build_sample_record,
    collect_telemetry,
    get_candidate_registers,
    main,
    parse_args,
)


class TestECDiscover(unittest.TestCase):
    """Test suite for tools/ec_discover.py."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

        # 1. Setup 256-byte EC fixture
        self.ec_bytes = bytearray(i for i in range(256))
        # Place recognizable bytes at candidate locations
        self.ec_bytes[0x03] = 0x21  # Baseline unlock
        self.ec_bytes[0x13] = 0xD9  # Baseline CPU readback
        self.ec_bytes[0x15] = 0x60  # Baseline GPU readback
        self.ec_bytes[0x21] = 0x10
        self.ec_bytes[0x22] = 0x04
        self.ec_bytes[0x37] = 0x32
        self.ec_bytes[0x3A] = 0x4B

        self.ec_file = self.root / "ec_test.bin"
        self.ec_file.write_bytes(self.ec_bytes)

        # 2. Setup synthetic sysfs tree
        self.sysfs_root = self.root / "sysfs"
        self.hwmon_base = self.sysfs_root / "sys" / "class" / "hwmon"
        self.pci_base = self.sysfs_root / "sys" / "bus" / "pci" / "devices"
        self.hwmon_base.mkdir(parents=True, exist_ok=True)
        self.pci_base.mkdir(parents=True, exist_ok=True)

        # coretemp
        coretemp_dir = self.hwmon_base / "hwmon0"
        coretemp_dir.mkdir(parents=True, exist_ok=True)
        (coretemp_dir / "name").write_text("coretemp\n")
        (coretemp_dir / "temp1_label").write_text("Package id 0\n")
        (coretemp_dir / "temp1_input").write_text("52000\n")

        # acer hwmon
        acer_dir = self.hwmon_base / "hwmon1"
        acer_dir.mkdir(parents=True, exist_ok=True)
        (acer_dir / "name").write_text("acer\n")
        (acer_dir / "fan1_input").write_text("2480\n")
        (acer_dir / "fan2_input").write_text("2350\n")
        (acer_dir / "temp1_input").write_text("45000\n")
        (acer_dir / "temp2_input").write_text("43000\n")
        (acer_dir / "temp3_input").write_text("41000\n")

        # 3. Setup test TOML config
        self.config_file = self.root / "config.toml"
        self.config_content = """
backend = "mock"
tick_s = 2.0
reassert_s = 10.0
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
speed_max = 0x64
read_register = 0x13

[ec.fans.gpu]
mode_register = 0x21
mode_auto = 0x10
mode_manual = 0x30
speed_register = 0x3A
speed_max = 0x64
read_register = 0x15

[curves]
cpu = [[45, 25], [88, 100]]
gpu = [[45, 25], [85, 100]]
"""
        self.config_file.write_text(self.config_content)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_candidate_register_extraction_from_arbitrary_config(self) -> None:
        # Build an arbitrary Config with unusual registers to verify no hardcoded sibling constants
        custom_fans = {
            "chassis": FanEC(
                mode_register=0x50,
                mode_auto=0x01,
                mode_manual=0x02,
                speed_register=0x60,
                speed_max=100,
                read_register=0x70,
            ),
            "aux": FanEC(
                mode_register=0x55,
                mode_auto=0x03,
                mode_manual=0x04,
                speed_register=0x65,
                speed_max=100,
                read_register=0x75,
            ),
        }
        custom_ec = ECConfig(
            unlock_register=0x09,
            unlock_value=0x99,
            write_allowlist=(0x09, 0x50, 0x55, 0x60, 0x65),
            fans=MappingProxyType(custom_fans),
        )
        custom_cfg = Config(
            backend="mock",
            tick_s=2.0,
            reassert_s=10.0,
            min_percent=25,
            emergency_c=90,
            startup_mode="auto",
            ec=custom_ec,
            curves=CurvesConfig(fans=MappingProxyType({})),
        )

        regs = get_candidate_registers(custom_cfg)
        # Exactly unlock (0x09) + 0x50, 0x60, 0x70 + 0x55, 0x65, 0x75
        expected = sorted([0x09, 0x50, 0x60, 0x70, 0x55, 0x65, 0x75])
        self.assertEqual(regs, expected)

    def test_candidate_register_extraction_rejects_missing_ec(self) -> None:
        empty_cfg = Config(
            backend="mock",
            tick_s=2.0,
            reassert_s=10.0,
            min_percent=25,
            emergency_c=90,
            startup_mode="auto",
            ec=None,
            curves=CurvesConfig(fans=MappingProxyType({})),
        )
        with self.assertRaises(ValueError) as ctx:
            get_candidate_registers(empty_cfg)
        self.assertIn("lacks a valid [ec] section", str(ctx.exception))

    def test_cli_argument_validation(self) -> None:
        # Invalid samples (< 1)
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = main(["--samples", "0"])
            self.assertEqual(rc, 1)
            self.assertIn("--samples must be >= 1", err.getvalue())

        with patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = main(["--samples", "-3"])
            self.assertEqual(rc, 1)
            self.assertIn("--samples must be >= 1", err.getvalue())

        # Invalid interval (<= 0 or non-finite)
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = main(["--interval", "0"])
            self.assertEqual(rc, 1)
            self.assertIn("--interval must be a positive finite number", err.getvalue())

        with patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = main(["--interval", "-2.5"])
            self.assertEqual(rc, 1)
            self.assertIn("--interval must be a positive finite number", err.getvalue())

        with patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = main(["--interval", "nan"])
            self.assertEqual(rc, 1)
            self.assertIn("--interval must be a positive finite number", err.getvalue())

        with patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = main(["--interval", "inf"])
            self.assertEqual(rc, 1)
            self.assertIn("--interval must be a positive finite number", err.getvalue())

    def test_missing_config_fails_nonzero(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = main(["--config", "/nonexistent/config.toml"])
            self.assertEqual(rc, 1)
            self.assertIn("not found", err.getvalue())

    def test_jsonl_smoke_candidate_snapshot(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = main([
                "--ec-path", str(self.ec_file),
                "--config", str(self.config_file),
                "--sysfs-root", str(self.sysfs_root),
                "--samples", "2",
                "--interval", "0.01",
                "--label", "idle-phase",
            ])

        self.assertEqual(rc, 0)
        lines = stdout.getvalue().strip().split("\n")
        self.assertEqual(len(lines), 2)

        sample0 = json.loads(lines[0])
        self.assertEqual(sample0["label"], "idle-phase")
        self.assertEqual(sample0["ec_path"], str(self.ec_file))
        self.assertIn("timestamp", sample0)
        self.assertIn("elapsed_s", sample0)
        self.assertGreaterEqual(sample0["elapsed_s"], 0.0)
        self.assertGreaterEqual(sample0["sample_duration_s"], 0.0)

        # Registers should only contain the candidate registers (7 keys: 0x03, 0x13, 0x15, 0x21, 0x22, 0x37, 0x3a)
        regs = sample0["registers"]
        self.assertEqual(len(regs), 7)
        self.assertEqual(regs["0x03"], 0x21)
        self.assertEqual(regs["0x13"], 0xD9)
        self.assertEqual(regs["0x15"], 0x60)
        self.assertEqual(regs["0x21"], 0x10)
        self.assertEqual(regs["0x22"], 0x04)
        self.assertEqual(regs["0x37"], 0x32)
        self.assertEqual(regs["0x3a"], 0x4B)

        # Candidate roles marked UNVERIFIED with raw bytes (unscaled)
        candidate = sample0["candidate_roles_unverified"]
        self.assertEqual(candidate["unlock"]["register"], "0x03")
        self.assertEqual(candidate["unlock"]["raw_value"], 0x21)
        self.assertFalse(candidate["unlock"]["verified"])
        self.assertEqual(candidate["unlock"]["status"], "UNVERIFIED")

        cpu_cand = candidate["fans"]["cpu"]
        self.assertEqual(cpu_cand["mode_register"], "0x22")
        self.assertEqual(cpu_cand["mode_raw_value"], 0x04)
        self.assertEqual(cpu_cand["speed_register"], "0x37")
        self.assertEqual(cpu_cand["speed_raw_value"], 0x32)
        self.assertEqual(cpu_cand["readback_register"], "0x13")
        self.assertEqual(cpu_cand["readback_raw_value"], 0xD9)
        self.assertFalse(cpu_cand["verified"])
        self.assertEqual(cpu_cand["status"], "UNVERIFIED")

        # Telemetry: real coretemp, acer temps, and fan RPM
        telemetry = sample0["telemetry"]
        self.assertEqual(telemetry["temperatures_c"]["cpu"], 52.0)
        self.assertEqual(telemetry["temperatures_c"]["acer_temp1"], 45.0)
        self.assertEqual(telemetry["temperatures_c"]["acer_temp2"], 43.0)
        self.assertEqual(telemetry["temperatures_c"]["acer_temp3"], 41.0)
        self.assertEqual(telemetry["fans_rpm"]["fan1"], 2480)
        self.assertEqual(telemetry["fans_rpm"]["fan2"], 2350)

        # GPU is not present in fake sysfs, so None with diagnostic, NOT zero
        self.assertIsNone(telemetry["temperatures_c"]["gpu"])
        self.assertIn("gpu_temperature", telemetry["diagnostics"])

    def test_full_dump_reads_all_256_registers(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = main([
                "--ec-path", str(self.ec_file),
                "--config", str(self.config_file),
                "--sysfs-root", str(self.sysfs_root),
                "--full-dump",
                "--samples", "1",
            ])

        self.assertEqual(rc, 0)
        sample = json.loads(stdout.getvalue().strip())
        regs = sample["registers"]
        self.assertEqual(len(regs), 256)
        for i in range(256):
            key = f"0x{i:02x}"
            self.assertIn(key, regs)
            self.assertEqual(regs[key], self.ec_bytes[i])

    def test_changing_fixture_values_across_samples(self) -> None:
        # We can update the file content between sample 1 and sample 2
        stdout = io.StringIO()
        stderr = io.StringIO()

        call_count = 0

        def mutate_ec_on_sleep(sec: float) -> None:
            nonlocal call_count
            call_count += 1
            # Modify register 0x37 in the file during sleep
            self.ec_bytes[0x37] = 0x64  # change speed to 100
            self.ec_file.write_bytes(self.ec_bytes)

        with patch("time.sleep", side_effect=mutate_ec_on_sleep):
            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                rc = main([
                    "--ec-path", str(self.ec_file),
                    "--config", str(self.config_file),
                    "--sysfs-root", str(self.sysfs_root),
                    "--samples", "2",
                    "--interval", "1.0",
                ])

        self.assertEqual(rc, 0)
        lines = stdout.getvalue().strip().split("\n")
        self.assertEqual(len(lines), 2)
        s1 = json.loads(lines[0])
        s2 = json.loads(lines[1])
        self.assertEqual(s1["registers"]["0x37"], 0x32)
        self.assertEqual(s2["registers"]["0x37"], 0x64)
        self.assertEqual(
            s1["candidate_roles_unverified"]["fans"]["cpu"]["speed_raw_value"],
            0x32,
        )
        self.assertEqual(
            s2["candidate_roles_unverified"]["fans"]["cpu"]["speed_raw_value"],
            0x64,
        )

    def test_ec_read_failure_fails_nonzero_and_emits_no_partial_snapshot(self) -> None:
        # Create a truncated EC file (only 10 bytes) so offset 0x13 fails
        short_file = self.root / "truncated_ec.bin"
        short_file.write_bytes(b"\x00" * 10)

        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = main([
                "--ec-path", str(short_file),
                "--config", str(self.config_file),
                "--sysfs-root", str(self.sysfs_root),
                "--samples", "1",
            ])

        self.assertEqual(rc, 1)
        self.assertIn("Short read at EC offset", stderr.getvalue())
        # stdout MUST be empty: no partial or corrupt snapshot emitted
        self.assertEqual(stdout.getvalue(), "")

    def test_keyboard_interrupt_exits_cleanly(self) -> None:
        with patch("tools.ec_discover.ECReader.open", side_effect=KeyboardInterrupt):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                rc = main([
                    "--ec-path", str(self.ec_file),
                    "--config", str(self.config_file),
                ])
            self.assertEqual(rc, 130)
            self.assertEqual(stdout.getvalue(), "")

    def test_missing_hwmon_device_yields_none_with_diagnostic(self) -> None:
        # Point to empty sysfs root where acer hwmon is absent
        empty_sysfs = self.root / "empty_sysfs"
        empty_sysfs.mkdir(parents=True, exist_ok=True)

        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = main([
                "--ec-path", str(self.ec_file),
                "--config", str(self.config_file),
                "--sysfs-root", str(empty_sysfs),
                "--samples", "1",
            ])

        self.assertEqual(rc, 0)
        sample = json.loads(stdout.getvalue().strip())
        telemetry = sample["telemetry"]
        self.assertIsNone(telemetry["fans_rpm"]["fan1"])
        self.assertIsNone(telemetry["fans_rpm"]["fan2"])
        self.assertIn("hwmon", telemetry["diagnostics"])

    def test_unreadable_tachometer_has_diagnostic_and_precedes_gpu_query(self) -> None:
        events = []
        backend = MagicMock()
        backend.is_open = True
        backend.fans = ["fan1"]
        backend.read_rpm.side_effect = lambda fan: events.append("rpm")

        def temperatures():
            events.append("temperatures")
            return {"cpu": 50.0, "gpu": None}

        telemetry = collect_telemetry(temperatures, backend)
        self.assertEqual(events, ["rpm", "temperatures"])
        self.assertIsNone(telemetry["fans_rpm"]["fan1"])
        self.assertIn("hwmon_fan1", telemetry["diagnostics"])
