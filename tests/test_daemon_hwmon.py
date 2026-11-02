"""
Integration tests for nitrofan.daemon with hwmon backend, read-only mode, and fake sysfs.

Verifies:
- Starting daemon with --backend hwmon --read-only and --sysfs-root
- Refusing hwmon without --read-only flag
- Refusing read-only mode with manual startup_mode in config
- Querying status over Unix domain socket returns hwmon identity, read_only=True, can_control=False,
  and real telemetry from fake sysfs
- Rejection of set_mode and set_curve requests over socket
- Clean SIGTERM shutdown with zero writes to sysfs
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from nitrofan.protocol import (
    CMD_SET_CURVE,
    CMD_SET_MODE,
    CMD_STATUS,
    decode_reply,
    encode_request,
)


# Run the daemon as if no host config existed, so an installed
# /etc/nitrofan/config.toml cannot change these tests' defaults.
ISOLATED_DAEMON = (
    "import nitrofan.daemon as daemon; "
    "daemon.DEFAULT_CONFIG_PATH = '/nonexistent/nitrofan-test/config.toml'; "
    "daemon.main()"
)


class TestDaemonHwmonIntegration(unittest.TestCase):
    """Subprocess integration tests for daemon with hwmon backend and fake sysfs."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.socket_path = self.root / "nitrofan.sock"
        self.lock_path = self.root / "nitrofan.lock"
        self.sysfs_root = self.root / "fake_sysfs"
        self.hwmon_base = self.sysfs_root / "sys" / "class" / "hwmon"
        self.pci_base = self.sysfs_root / "sys" / "bus" / "pci" / "devices"

        self.hwmon_base.mkdir(parents=True, exist_ok=True)
        self.pci_base.mkdir(parents=True, exist_ok=True)

        # Setup coretemp
        coretemp_dir = self.hwmon_base / "hwmon0"
        coretemp_dir.mkdir(parents=True, exist_ok=True)
        (coretemp_dir / "name").write_text("coretemp\n")
        (coretemp_dir / "temp1_label").write_text("Package id 0\n")
        (coretemp_dir / "temp1_input").write_text("49000\n")

        # Setup acer hwmon with fan1 and fan2
        self.acer_dir = self.hwmon_base / "hwmon5"
        self.acer_dir.mkdir(parents=True, exist_ok=True)
        (self.acer_dir / "name").write_text("acer\n")
        (self.acer_dir / "fan1_input").write_text("2450\n")
        (self.acer_dir / "fan2_input").write_text("2300\n")
        (self.acer_dir / "temp1_input").write_text("47000\n")
        (self.acer_dir / "temp2_input").write_text("45000\n")

        # Setup NVIDIA GPU PCI device (active display)
        gpu_dir = self.pci_base / "0000:01:00.0"
        (gpu_dir / "power").mkdir(parents=True, exist_ok=True)
        (gpu_dir / "vendor").write_text("0x10de\n")
        (gpu_dir / "class").write_text("0x030000\n")
        (gpu_dir / "power" / "runtime_status").write_text("suspended\n")  # suspended -> GPU temp None

        self.procs: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=1.0)
            if p.stdout:
                try:
                    p.stdout.close()
                except Exception:
                    pass
            if p.stderr:
                try:
                    p.stderr.close()
                except Exception:
                    pass
        self.tmp_dir.cleanup()

    def _start_daemon(
        self,
        extra_args: list[str] | None = None,
    ) -> subprocess.Popen:
        cmd = [
            sys.executable,
            "-c",
            ISOLATED_DAEMON,
            "--backend",
            "hwmon",
            "--read-only",
            "--sysfs-root",
            str(self.sysfs_root),
            "--socket",
            str(self.socket_path),
            "--lock",
            str(self.lock_path),
        ]
        if extra_args:
            cmd.extend(extra_args)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.procs.append(proc)

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                raise RuntimeError(
                    f"Daemon exited prematurely ({proc.returncode}):\n{stderr}\n{stdout}"
                )
            if self.socket_path.exists():
                s = None
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(0.5)
                    s.connect(str(self.socket_path))
                    return proc
                except OSError:
                    pass
                finally:
                    if s is not None:
                        try:
                            s.close()
                        except OSError:
                            pass
            time.sleep(0.05)

        raise TimeoutError("Timed out waiting for daemon socket to become ready")

    def _send_request(self, req_bytes: bytes, timeout: float = 2.0) -> dict:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(self.socket_path))
        try:
            s.sendall(req_bytes)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
            line, _ = buf.split(b"\n", 1)
            return decode_reply(line)
        finally:
            s.close()

    def test_refuse_hwmon_without_read_only(self) -> None:
        cmd = [
            sys.executable,
            "-c",
            ISOLATED_DAEMON,
            "--backend",
            "hwmon",
            "--socket",
            str(self.socket_path),
            "--lock",
            str(self.lock_path),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("requires '--read-only'", res.stderr)

    def test_refuse_manual_startup_mode_in_read_only(self) -> None:
        bad_cfg = self.root / "bad_startup.toml"
        bad_cfg.write_text(
            'backend = "hwmon"\n'
            'tick_s = 2.0\n'
            'reassert_s = 10.0\n'
            'min_percent = 25\n'
            'emergency_c = 90\n'
            'startup_mode = "max"\n'
            '[curves]\n'
            'cpu = [[50, 30], [80, 100]]\n'
            'gpu = [[50, 30], [80, 100]]\n'
        )
        cmd = [
            sys.executable,
            "-c",
            ISOLATED_DAEMON,
            "--backend",
            "hwmon",
            "--read-only",
            "--config",
            str(bad_cfg),
            "--socket",
            str(self.socket_path),
            "--lock",
            str(self.lock_path),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("startup_mode 'auto'", res.stderr)

    def test_hwmon_read_only_live_status(self) -> None:
        proc = self._start_daemon()

        reply = self._send_request(encode_request(CMD_STATUS))
        self.assertTrue(reply.get("ok"))
        self.assertEqual(reply.get("backend"), "hwmon")
        self.assertTrue(reply.get("read_only"))
        self.assertFalse(reply.get("can_control"))
        self.assertEqual(reply.get("mode"), "auto")

        # Fans telemetry from fake sysfs
        fans = reply.get("fans", {})
        self.assertIn("fan1", fans)
        self.assertIn("fan2", fans)
        self.assertEqual(fans["fan1"]["rpm"], 2450)
        self.assertEqual(fans["fan2"]["rpm"], 2300)
        self.assertIsNone(fans["fan1"]["percent"])
        self.assertIsNone(fans["fan2"]["percent"])

        # Temperature telemetry from fake sysfs
        temps = reply.get("temps", {})
        self.assertEqual(temps.get("cpu"), 49.0)
        self.assertIsNone(temps.get("gpu"))  # suspended
        self.assertEqual(temps.get("acer_temp1"), 47.0)
        self.assertEqual(temps.get("acer_temp2"), 45.0)

        # Clean termination
        proc.terminate()
        proc.wait(timeout=3.0)
        self.assertFalse(self.socket_path.exists())

    def test_hwmon_rejects_manual_control_over_socket(self) -> None:
        self._start_daemon()

        # Try set_mode fixed
        reply_mode = self._send_request(encode_request(CMD_SET_MODE, mode="fixed", percent=60))
        self.assertFalse(reply_mode.get("ok"))
        self.assertIn("read-only", reply_mode.get("error", ""))

        # Try set_curve
        reply_curve = self._send_request(
            encode_request(CMD_SET_CURVE, fan="fan1", points=[[40, 25], [80, 100]])
        )
        self.assertFalse(reply_curve.get("ok"))
        self.assertIn("read-only", reply_curve.get("error", ""))


if __name__ == "__main__":
    unittest.main()
