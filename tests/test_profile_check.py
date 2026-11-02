"""
test_profile_check.py - Tests for tools/profile_check.py developer diagnostic tool.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_CHECK_SCRIPT = REPO_ROOT / "tools" / "profile_check.py"


class TestProfileCheckCLI(unittest.TestCase):
    """Test CLI behavior of tools/profile_check.py."""

    def _run_cli(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, str(PROFILE_CHECK_SCRIPT)] + args
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
        )

    def test_list_human_readable(self) -> None:
        proc = self._run_cli(["--list"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("NitroFan Model Catalogue", proc.stdout)
        self.assertIn("AN515-54", proc.stdout)
        self.assertIn("AN515-55", proc.stdout)
        self.assertIn("guarded_max_trial_eligible", proc.stdout)
        self.assertIn("needs_profile", proc.stdout)

    def test_list_json(self) -> None:
        proc = self._run_cli(["--list", "--json"])
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(data["upstream"]["version"], "0.5.4")
        self.assertEqual(data["upstream"]["commit"], "9da010d6a2e8351bdf6900d6c3643f1e742af936")
        models = {m["model_id"]: m for m in data["models"]}
        self.assertEqual(len(models), 11)
        self.assertIn("AN515-54", models)
        self.assertTrue(models["AN515-54"]["capabilities"]["guarded_max_trial"])
        self.assertFalse(models["AN515-54"]["capabilities"]["production_control"])
        self.assertFalse(models["AN515-55"]["capabilities"]["guarded_max_trial"])

    def test_model_detail_success(self) -> None:
        proc = self._run_cli(["--model", "AN515-54", "--json"])
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(data["model_id"], "AN515-54")
        self.assertEqual(data["support_status"], "guarded_max_trial_eligible")
        self.assertTrue(data["capabilities"]["guarded_max_trial"])

    def test_model_detail_not_found(self) -> None:
        proc = self._run_cli(["--model", "AN515-99", "--json"])
        self.assertEqual(proc.returncode, 1)
        data = json.loads(proc.stdout)
        self.assertIn("error", data)

    def test_detect_against_synthetic_an515_54_v133(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dmi_dir = Path(td)
            (dmi_dir / "sys_vendor").write_text("Acer\n")
            (dmi_dir / "product_name").write_text("Nitro AN515-54\n")
            (dmi_dir / "bios_version").write_text("V1.33\n")

            proc = self._run_cli(["--detect", "--dmi-root", str(dmi_dir), "--json"])
            self.assertEqual(proc.returncode, 0)
            data = json.loads(proc.stdout)
            self.assertTrue(data["matched"])
            self.assertEqual(data["model_id"], "AN515-54")
            self.assertEqual(data["support_status"], "guarded_max_trial_eligible")
            self.assertTrue(data["capabilities"]["guarded_max_trial"])
            self.assertFalse(data["capabilities"]["production_control"])
            self.assertTrue(data["bios_matched"])

    def test_detect_against_synthetic_wrong_bios(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dmi_dir = Path(td)
            (dmi_dir / "sys_vendor").write_text("Acer\n")
            (dmi_dir / "product_name").write_text("Nitro AN515-54\n")
            (dmi_dir / "bios_version").write_text("V1.30\n")

            proc = self._run_cli(["--detect", "--dmi-root", str(dmi_dir), "--json"])
            self.assertEqual(proc.returncode, 0)
            data = json.loads(proc.stdout)
            self.assertTrue(data["matched"])
            self.assertEqual(data["model_id"], "AN515-54")
            self.assertEqual(data["support_status"], "upstream_only")
            self.assertFalse(data["capabilities"]["guarded_max_trial"])
            self.assertFalse(data["bios_matched"])
            self.assertIn("V1.30", data["next_steps"])

    def test_detect_against_synthetic_needs_profile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dmi_dir = Path(td)
            (dmi_dir / "sys_vendor").write_text("Acer\n")
            (dmi_dir / "product_name").write_text("Nitro AN515-55\n")
            (dmi_dir / "bios_version").write_text("V1.00\n")

            proc = self._run_cli(["--detect", "--dmi-root", str(dmi_dir), "--json"])
            self.assertEqual(proc.returncode, 0)
            data = json.loads(proc.stdout)
            self.assertTrue(data["matched"])
            self.assertEqual(data["model_id"], "AN515-55")
            self.assertEqual(data["support_status"], "needs_profile")
            self.assertFalse(data["capabilities"]["guarded_max_trial"])

    def test_detect_against_synthetic_unsupported_vendor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dmi_dir = Path(td)
            (dmi_dir / "sys_vendor").write_text("Dell\n")
            (dmi_dir / "product_name").write_text("Nitro AN515-54\n")
            (dmi_dir / "bios_version").write_text("V1.33\n")

            proc = self._run_cli(["--detect", "--dmi-root", str(dmi_dir), "--json"])
            self.assertEqual(proc.returncode, 0)
            data = json.loads(proc.stdout)
            self.assertFalse(data["matched"])
            self.assertEqual(data["support_status"], "unsupported")
            self.assertFalse(data["capabilities"]["guarded_max_trial"])


if __name__ == "__main__":
    unittest.main()
