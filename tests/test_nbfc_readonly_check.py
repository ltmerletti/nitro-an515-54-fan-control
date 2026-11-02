"""Unit tests for tools/nbfc_readonly_check.sh preflight checks and guards."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "tools" / "nbfc_readonly_check.sh"


class TestNBFCReadOnlyCheckScript(unittest.TestCase):
    def test_script_syntax(self) -> None:
        """Verify bash syntax via bash -n."""
        res = subprocess.run(["bash", "-n", str(SCRIPT_PATH)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Syntax check failed: {res.stderr}")

    def test_fails_as_non_root_by_default(self) -> None:
        """Verify script exits with error when run without root."""
        env = os.environ.copy()
        env["NBFC_ALLOW_NON_ROOT"] = "0"
        res = subprocess.run(["bash", str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("requires root privileges", res.stderr)

    def test_fails_on_dmi_mismatch(self) -> None:
        """Verify script aborts when DMI does not match Nitro AN515-54."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dmi_dir = Path(tmpdir)
            prod_file = dmi_dir / "product_name"
            vendor_file = dmi_dir / "sys_vendor"
            prod_file.write_text("ThinkPad X1\n")
            vendor_file.write_text("Lenovo\n")

            env = os.environ.copy()
            env["NBFC_ALLOW_NON_ROOT"] = "1"
            env["NBFC_SYSFS_DMI_PRODUCT"] = str(prod_file)
            env["NBFC_SYSFS_DMI_VENDOR"] = str(vendor_file)

            res = subprocess.run(["bash", str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("DMI mismatch", res.stderr)

    def test_rejects_zero_or_unbounded_duration(self) -> None:
        for duration in ("0", "-1", "121", "nan", "0.1", "999999999999999999999"):
            with self.subTest(duration=duration):
                env = os.environ.copy()
                env["NBFC_ALLOW_NON_ROOT"] = "1"
                env["NBFC_TRIAL_DURATION"] = duration
                res = subprocess.run(["bash", str(SCRIPT_PATH)], env=env,
                                     capture_output=True, text=True, timeout=5)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("REAL_DURATION must be a whole number", res.stderr)

    def test_early_success_exit_does_not_validate_the_trial(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package_bin = root / "package/usr/bin"
            profile = root / "package/usr/share/nbfc/configs/Acer Nitro AN515-54.json"
            library = root / "dependencies/usr/lib/x86_64-linux-gnu/liblua5.4.so.0"
            commands = root / "commands"
            package_bin.mkdir(parents=True)
            commands.mkdir()
            for name, source in {
                package_bin / "nbfc_service": "#!/bin/sh\nexit 0\n",
                package_bin / "nbfc": "#!/bin/sh\nexit 0\n",
                commands / "pgrep": "#!/bin/sh\nexit 1\n",
                commands / "timeout": "#!/bin/sh\nshift 5\nexec \"$@\"\n",
            }.items():
                name.write_text(source)
                name.chmod(0o755)
            for file in (profile, library):
                file.parent.mkdir(parents=True, exist_ok=True)
                file.touch()
            fixtures = {"NBFC_SYSFS_DMI_PRODUCT": "Nitro AN515-54",
                        "NBFC_SYSFS_DMI_VENDOR": "Acer",
                        "NBFC_SYSFS_WRITE_SUPPORT": "N",
                        "NBFC_SYSFS_EC_IO": ""}
            env = os.environ.copy()
            env["NBFC_ALLOW_NON_ROOT"] = "1"
            env["NBFC_BASE_DIR"] = str(root)
            env["PATH"] = str(commands) + os.pathsep + env["PATH"]
            for key, value in fixtures.items():
                file = root / key
                file.write_text(value)
                env[key] = str(file)
            res = subprocess.run(["bash", str(SCRIPT_PATH)], env=env,
                                 capture_output=True, text=True, timeout=5)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("dummy trial ended before its full duration (exit 0)", res.stderr)
            self.assertNotIn("Phase 2", res.stdout)

    def test_fails_on_write_support_enabled(self) -> None:
        """Verify script aborts if write_support is 'Y'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            prod_file = td / "product_name"
            vendor_file = td / "sys_vendor"
            ws_file = td / "write_support"

            prod_file.write_text("Nitro AN515-54\n")
            vendor_file.write_text("Acer\n")
            ws_file.write_text("Y\n")

            env = os.environ.copy()
            env["NBFC_ALLOW_NON_ROOT"] = "1"
            env["NBFC_SYSFS_DMI_PRODUCT"] = str(prod_file)
            env["NBFC_SYSFS_DMI_VENDOR"] = str(vendor_file)
            env["NBFC_SYSFS_WRITE_SUPPORT"] = str(ws_file)

            res = subprocess.run(["bash", str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Refusing to proceed with write-enabled module", res.stderr)

    def test_fails_on_missing_binaries(self) -> None:
        """Verify script aborts if NBFC binaries or libraries are missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            prod_file = td / "product_name"
            vendor_file = td / "sys_vendor"
            ws_file = td / "write_support"
            ec_file = td / "ec_io"

            prod_file.write_text("Nitro AN515-54\n")
            vendor_file.write_text("Acer\n")
            ws_file.write_text("N\n")
            ec_file.touch()

            env = os.environ.copy()
            env["NBFC_ALLOW_NON_ROOT"] = "1"
            env["NBFC_SYSFS_DMI_PRODUCT"] = str(prod_file)
            env["NBFC_SYSFS_DMI_VENDOR"] = str(vendor_file)
            env["NBFC_SYSFS_WRITE_SUPPORT"] = str(ws_file)
            env["NBFC_SYSFS_EC_IO"] = str(ec_file)
            env["NBFC_BASE_DIR"] = str(td / "nonexistent")

            res = subprocess.run(["bash", str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Required file missing", res.stderr)


if __name__ == "__main__":
    unittest.main()
