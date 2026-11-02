"""
Unit tests for nitrofan.ec -- ECReader.

Tests read-only EC access: O_RDONLY open flags, exact os.pread offset,
rejection of invalid offsets, context manager cleanup, idempotent close,
short-read rejection, no invented zeros, and clear permission/missing path errors.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nitrofan.ec import DEFAULT_DEBUGFS_EC, DEFAULT_DEV_EC, ECReader


class TestECReader(unittest.TestCase):
    """Test ECReader behavior, flags, offsets, and error handling."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        # Create a synthetic 256-byte EC fixture
        self.fixture_bytes = bytes(i for i in range(256))
        self.fixture_file = self.root / "ec_fixture.bin"
        self.fixture_file.write_bytes(self.fixture_bytes)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_initial_state(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        self.assertFalse(reader.is_open)
        self.assertIsNone(reader.path)

    def test_open_and_close_idempotent(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        reader.open()
        self.assertTrue(reader.is_open)
        self.assertEqual(reader.path, str(self.fixture_file))

        # Second open is a no-op
        reader.open()
        self.assertTrue(reader.is_open)

        reader.close()
        self.assertFalse(reader.is_open)

        # Second close is a safe no-op
        reader.close()
        self.assertFalse(reader.is_open)

    def test_context_manager(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with reader as r:
            self.assertIs(r, reader)
            self.assertTrue(reader.is_open)
            byte_val = reader.read_byte(0x20)
            self.assertEqual(byte_val, 0x20)
        self.assertFalse(reader.is_open)

    def test_context_manager_cleanup_on_exception(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with self.assertRaises(ZeroDivisionError):
            with reader:
                self.assertTrue(reader.is_open)
                _ = 1 / 0
        self.assertFalse(reader.is_open)

    def test_open_flags_are_strictly_read_only(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with patch("os.open", wraps=os.open) as mock_open:
            reader.open()
            try:
                mock_open.assert_called_once()
                path_arg, flags_arg = mock_open.call_args[0][:2]
                self.assertEqual(path_arg, str(self.fixture_file))
                # Verify access mode is O_RDONLY and neither O_WRONLY nor O_RDWR is set
                self.assertEqual(flags_arg & os.O_ACCMODE, os.O_RDONLY)
                self.assertFalse(flags_arg & os.O_WRONLY)
                self.assertFalse(flags_arg & os.O_RDWR)
                if hasattr(os, "O_CLOEXEC"):
                    self.assertTrue(flags_arg & os.O_CLOEXEC)
            finally:
                reader.close()

    def test_default_path_selection(self) -> None:
        # 1. When /dev/ec exists
        with patch("os.path.exists", side_effect=lambda p: p == DEFAULT_DEV_EC):
            reader = ECReader()
            self.assertEqual(reader._resolve_path(), DEFAULT_DEV_EC)

        # 2. When /dev/ec does not exist
        with patch("os.path.exists", return_value=False):
            reader = ECReader()
            self.assertEqual(reader._resolve_path(), DEFAULT_DEBUGFS_EC)

        # 3. Explicit path overrides defaults
        reader = ECReader(ec_path="/custom/path")
        self.assertEqual(reader._resolve_path(), "/custom/path")

    def test_missing_file_raises_descriptive_error(self) -> None:
        # Custom non-existent path
        reader = ECReader(ec_path="/nonexistent/ec/path")
        with self.assertRaises(FileNotFoundError) as ctx:
            reader.open()
        self.assertIn("/nonexistent/ec/path", str(ctx.exception))

        # Default path missing gives ec_sys module suggestion
        with patch("os.path.exists", return_value=False):
            with patch("os.open", side_effect=FileNotFoundError("No such file")):
                default_reader = ECReader()
                with self.assertRaises(FileNotFoundError) as ctx:
                    default_reader.open()
                self.assertIn("ec_sys", str(ctx.exception))
                self.assertFalse(default_reader.is_open)

    def test_permission_error_raises_clear_error(self) -> None:
        with patch("os.open", side_effect=PermissionError("Permission denied")):
            reader = ECReader(ec_path="/sys/kernel/debug/ec/ec0/io")
            with self.assertRaises(PermissionError) as ctx:
                reader.open()
            self.assertIn("Root/sudo privileges are required", str(ctx.exception))
            self.assertFalse(reader.is_open)

    def test_read_byte_exact_pread_offset(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with reader:
            with patch("os.pread", wraps=os.pread) as mock_pread:
                val = reader.read_byte(0x37)
                self.assertEqual(val, 0x37)
                mock_pread.assert_called_once_with(reader._fd, 1, 0x37)

    def test_read_byte_not_open_raises_runtime_error(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with self.assertRaises(RuntimeError) as ctx:
            reader.read_byte(0x00)
        self.assertIn("not open", str(ctx.exception))

    def test_validate_offset_rejects_invalid_offsets(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with reader:
            invalid_offsets = [-1, 256, 1000, 1.5, "0x10", True, False, None]
            for bad in invalid_offsets:
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        reader.read_byte(bad)  # type: ignore[arg-type]

    def test_short_read_raises_runtime_error(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with reader:
            # Simulate pread returning 0 bytes (EOF / short read)
            with patch("os.pread", return_value=b""):
                with self.assertRaises(RuntimeError) as ctx:
                    reader.read_byte(0x10)
                self.assertIn("Short read at EC offset 0x10", str(ctx.exception))
                self.assertIn("expected 1 byte, got 0", str(ctx.exception))

            # Simulate pread returning 2 bytes
            with patch("os.pread", return_value=b"\x01\x02"):
                with self.assertRaises(RuntimeError) as ctx:
                    reader.read_byte(0x10)
                self.assertIn("expected 1 byte, got 2", str(ctx.exception))

    def test_read_registers_bulk_snapshot(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with reader:
            regs = reader.read_registers([0x03, 0x13, 0x22, 0x37])
            self.assertEqual(regs, {
                0x03: 0x03,
                0x13: 0x13,
                0x22: 0x22,
                0x37: 0x37,
            })

    def test_read_all_reads_all_256_bytes(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with reader:
            all_bytes = reader.read_all()
            self.assertEqual(len(all_bytes), 256)
            for i in range(256):
                self.assertEqual(all_bytes[i], i)

    def test_no_invented_zero_on_read_failure(self) -> None:
        # File shorter than 256 bytes (only 32 bytes)
        short_file = self.root / "short_ec.bin"
        short_file.write_bytes(bytes(range(32)))

        reader = ECReader(ec_path=short_file)
        with reader:
            # Reading offset 31 succeeds
            self.assertEqual(reader.read_byte(31), 31)
            # Reading offset 32 fails with short read error; must NOT return 0
            with self.assertRaises(RuntimeError) as ctx:
                reader.read_registers([0x1F, 0x20])
            self.assertIn("Short read", str(ctx.exception))

    def test_read_u16_le_decodes_little_endian(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with reader:
            # self.fixture_bytes has byte i at offset i
            # offset 0x13 is 19, offset 0x14 is 20 -> 19 + 256 * 20 = 5139
            val = reader.read_u16_le(0x13, 0x14)
            self.assertEqual(val, 19 | (20 << 8))

    def test_read_u16_le_not_open_raises_runtime_error(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with self.assertRaises(RuntimeError) as ctx:
            reader.read_u16_le(0x13, 0x14)
        self.assertIn("not open", str(ctx.exception))

    def test_read_u16_le_offset_validation(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with reader:
            # Identical offsets
            with self.assertRaises(ValueError) as ctx:
                reader.read_u16_le(0x13, 0x13)
            self.assertIn("must be distinct", str(ctx.exception))

            # Out of bounds
            with self.assertRaises(ValueError):
                reader.read_u16_le(-1, 0x14)
            with self.assertRaises(ValueError):
                reader.read_u16_le(0x13, 256)
            with self.assertRaises(ValueError):
                reader.read_u16_le(True, 0x14)  # type: ignore[arg-type]

    def test_read_u16_le_rollover_retry_succeeds(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with reader:
            # Simulate high-low-high read where:
            # Attempt 1: high1=0x09, low=0x00, high2=0x0A (rollover torn read)
            # Attempt 2: high1=0x0A, low=0x01, high2=0x0A (stable)
            sequence = [0x09, 0x00, 0x0A, 0x0A, 0x01, 0x0A]
            with patch.object(reader, "read_byte", side_effect=sequence):
                val = reader.read_u16_le(0x13, 0x14, max_retries=3)
                self.assertEqual(val, 0x01 | (0x0A << 8))

    def test_read_u16_le_unstable_raises_runtime_error_no_fake_zero(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with reader:
            # Simulate high byte perpetually changing across 3 retries
            sequence = [
                0x01, 0x00, 0x02,  # retry 1
                0x02, 0x00, 0x03,  # retry 2
                0x03, 0x00, 0x04,  # retry 3
            ]
            with patch.object(reader, "read_byte", side_effect=sequence):
                with self.assertRaises(RuntimeError) as ctx:
                    reader.read_u16_le(0x13, 0x14, max_retries=3)
                self.assertIn("Unstable EC 16-bit read", str(ctx.exception))
                self.assertIn("across 3 retries", str(ctx.exception))

    def test_read_u16_le_short_read_propagates_immediately(self) -> None:
        reader = ECReader(ec_path=self.fixture_file)
        with reader:
            with patch("os.pread", return_value=b""):
                with self.assertRaises(RuntimeError) as ctx:
                    reader.read_u16_le(0x13, 0x14)
                self.assertIn("Short read", str(ctx.exception))

    def test_read_u16_le_replay_baseline_30_records(self) -> None:
        import json
        baseline_path = Path(__file__).resolve().parent.parent / "docs" / "data" / "ec-passive-baseline-2026-11-01.jsonl"
        if not baseline_path.is_file():
            self.skipTest("Baseline data file not found")

        records = [json.loads(line) for line in baseline_path.read_text().splitlines() if line.strip()]
        self.assertEqual(len(records), 30)

        fan1_exact = 0
        fan2_exact = 0
        fan1_diffs = []
        fan2_diffs = []

        replay_file = self.root / "replay.bin"
        for rec in records:
            # Build 256 byte binary from registers dict
            b = bytearray(256)
            for hex_key, val in rec["registers"].items():
                off = int(hex_key, 16)
                b[off] = val
            replay_file.write_bytes(b)

            reader = ECReader(ec_path=replay_file)
            with reader:
                fan1_rpm = reader.read_u16_le(0x13, 0x14)
                fan2_rpm = reader.read_u16_le(0x15, 0x16)

            hwmon_fan1 = rec["telemetry"]["fans_rpm"]["fan1"]
            hwmon_fan2 = rec["telemetry"]["fans_rpm"]["fan2"]

            # Decoded values are positive non-zero within observed hardware range
            self.assertGreaterEqual(fan1_rpm, 2380)
            self.assertLessEqual(fan1_rpm, 4000)
            self.assertGreaterEqual(fan2_rpm, 2255)
            self.assertLessEqual(fan2_rpm, 3846)

            diff1 = abs(fan1_rpm - hwmon_fan1)
            diff2 = abs(fan2_rpm - hwmon_fan2)
            fan1_diffs.append(diff1)
            fan2_diffs.append(diff2)
            if diff1 == 0:
                fan1_exact += 1
            if diff2 == 0:
                fan2_exact += 1

        self.assertEqual(fan1_exact, 27)
        self.assertEqual(fan2_exact, 29)
        self.assertLessEqual(max(fan1_diffs), 53)
        self.assertLessEqual(max(fan2_diffs), 25)
