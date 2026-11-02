"""Tests for the standalone read-only model probe."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools import probe_model

REFERENCE = Path(__file__).resolve().parent.parent / "docs" / "ec-baseline-2026-11-01.hex"


def reference_dump() -> bytearray:
    data = bytearray()
    for line in REFERENCE.read_text().splitlines():
        data.extend(int(b, 16) for b in line.split()[1:])
    return data[:256]


class ProbeFixture:
    def __init__(self, root: Path, product: str = "Nitro AN515-56", hwmon: bool = True):
        self.root = root
        dmi = root / "sys/class/dmi/id"
        dmi.mkdir(parents=True)
        (dmi / "sys_vendor").write_text("Acer\n")
        (dmi / "product_name").write_text(product + "\n")
        (dmi / "bios_version").write_text("V1.10\n")
        self.ec = root / "ec_io"
        self.data = reference_dump()
        self.hwmon = root / "sys/class/hwmon/hwmon3"
        self.hwmon.mkdir(parents=True)
        (self.hwmon / "name").write_text("acer\n" if hwmon else "other\n")
        self.set_rpm(2521, 2400)

    def set_rpm(self, fan1: int, fan2: int, hwmon_offset: int = 0) -> None:
        self.data[0x13:0x15] = fan1.to_bytes(2, "little")
        self.data[0x15:0x17] = fan2.to_bytes(2, "little")
        self.ec.write_bytes(bytes(self.data))
        (self.hwmon / "fan1_input").write_text(f"{fan1 + hwmon_offset}\n")
        (self.hwmon / "fan2_input").write_text(f"{fan2 + hwmon_offset}\n")

    def samples(self, rpms, hwmon_offset=0):
        fans = probe_model.find_hwmon_fans(self.root)
        out = []
        for fan1, fan2 in rpms:
            self.set_rpm(fan1, fan2, hwmon_offset)
            out.append(probe_model.sample(self.ec, fans, None, "idle"))
        return out


class TestProbeModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reference_dump_reads_as_plausible_rpm(self):
        data = bytes(reference_dump())
        self.assertEqual(probe_model.word(data, 0x13), 2521)
        self.assertEqual(probe_model.word(data, 0x15), 2400)

    def test_an515_54_layout_matches(self):
        fx = ProbeFixture(self.root)
        result = probe_model.analyse(fx.samples([(2521, 2400), (4100, 3900)]))
        self.assertEqual(result["verdict"], "matches_an515_54_layout")
        self.assertEqual(result["checks"]["reported_only"]["0x03"], 0x21)

    def test_hwmon_disagreement_is_a_different_layout(self):
        fx = ProbeFixture(self.root)
        result = probe_model.analyse(fx.samples([(2521, 2400), (4100, 3900)], hwmon_offset=1500))
        self.assertEqual(result["verdict"], "different_layout")

    def test_without_hwmon_fans_must_change_under_load(self):
        fx = ProbeFixture(self.root, hwmon=False)
        steady = probe_model.analyse(fx.samples([(2521, 2400), (2530, 2410)]))
        self.assertEqual(steady["verdict"], "different_layout")
        ramped = probe_model.analyse(fx.samples([(2521, 2400), (4800, 4500)]))
        self.assertEqual(ramped["verdict"], "matches_an515_54_layout")

    def test_implausible_words_are_rejected(self):
        fx = ProbeFixture(self.root, hwmon=False)
        result = probe_model.analyse(fx.samples([(2521, 40000), (4800, 50000)]))
        self.assertEqual(result["verdict"], "different_layout")

    def test_control_byte_difference_is_flagged(self):
        fx = ProbeFixture(self.root)
        fx.data[0x22] = 4
        result = probe_model.analyse(fx.samples([(2521, 2400), (4100, 3900)]))
        self.assertEqual(result["verdict"], "fan_readings_match_control_differs")
        self.assertFalse(result["checks"]["control_bytes"]["registers"]["0x22"]["ok"])

    def test_cli_report_is_read_only_and_complete(self):
        fx = ProbeFixture(self.root)
        before = fx.ec.read_bytes()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = probe_model.main(["--root", str(self.root), "--ec-path", str(fx.ec),
                                     "--idle", "0", "--load", "0", "--cool", "0", "--interval", "0"])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["identity"]["product_name"], "Nitro AN515-56")
        self.assertEqual(report["verdict"], "matches_an515_54_layout")
        self.assertEqual(len(report["samples"]), 3)
        self.assertEqual(fx.ec.read_bytes(), before)
        self.assertIn("No EC writes", err.getvalue())

    def test_source_never_opens_ec_for_writing(self):
        source = Path(probe_model.__file__).read_text()
        for forbidden in ("O_WRONLY", "O_RDWR", "pwrite", "write_support=1", '"wb"', '"r+'):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
