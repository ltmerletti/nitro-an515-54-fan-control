"""
Integration tests for the NitroFan daemon process and Unix domain socket IPC.
"""

import json
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
    CMD_SUBSCRIBE,
    MAX_FRAME_BYTES,
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


class TestDaemonIntegration(unittest.TestCase):
    """Subprocess integration tests for daemon socket protocol and lifecycle."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.socket_path = self.root / "nitrofan.sock"
        self.lock_path = self.root / "nitrofan.lock"
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
        backend: str | None = "mock",
        config_path: Path | None = None,
        extra_args: list[str] | None = None,
    ) -> subprocess.Popen:
        """Start daemon as an unprivileged subprocess and wait for socket readiness."""
        cmd = [
            sys.executable,
            "-c",
            ISOLATED_DAEMON,
            "--socket",
            str(self.socket_path),
            "--lock",
            str(self.lock_path),
        ]
        if backend is not None:
            cmd.extend(["--backend", backend])
        if config_path is not None:
            cmd.extend(["--config", str(config_path)])
        if extra_args:
            cmd.extend(extra_args)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.procs.append(proc)

        # Wait for socket to be created and accepting connections
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
        """Send a request over Unix socket and return decoded reply dict."""
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

    def test_unprivileged_startup_and_status(self) -> None:
        proc = self._start_daemon()
        reply = self._send_request(encode_request(CMD_STATUS))

        self.assertTrue(reply.get("ok"))
        self.assertEqual(reply.get("backend"), "mock")
        self.assertEqual(reply.get("mode"), "auto")
        self.assertIn("cpu", reply.get("fans", {}))
        self.assertIn("gpu", reply.get("fans", {}))
        self.assertIn("cpu", reply.get("temps", {}))
        self.assertIn("gpu", reply.get("temps", {}))
        self.assertIn("uptime", reply)
        self.assertIn("curves", reply)

        # Clean SIGTERM shutdown
        proc.terminate()
        ret = proc.wait(timeout=3.0)
        self.assertEqual(ret, 0)
        # Socket must be unlinked
        self.assertFalse(self.socket_path.exists())

    def test_set_mode_and_set_curve(self) -> None:
        self._start_daemon()

        # Set fixed mode 50%
        req = encode_request(CMD_SET_MODE, mode="fixed", percent=50)
        reply = self._send_request(req)
        self.assertTrue(reply.get("ok"))

        # Check status reflects fixed mode
        status = self._send_request(encode_request(CMD_STATUS))
        self.assertEqual(status.get("mode"), "fixed")
        fans = status.get("fans", {})
        self.assertEqual(fans["cpu"]["percent"], 50)
        self.assertEqual(fans["gpu"]["percent"], 50)

        # Set custom curve for CPU
        new_points = [[40, 25], [65, 50], [85, 100]]
        req_curve = encode_request(CMD_SET_CURVE, fan="cpu", points=new_points)
        reply_curve = self._send_request(req_curve)
        self.assertTrue(reply_curve.get("ok"))

        # Verify curves in status
        status2 = self._send_request(encode_request(CMD_STATUS))
        curves = status2.get("curves", {})
        self.assertEqual(curves["cpu"], [[40.0, 25], [65.0, 50], [85.0, 100]])

    def test_subscribe_streaming(self) -> None:
        # Start with short tick interval using custom config
        cfg_file = self.root / "fast_tick.toml"
        cfg_file.write_text(
            'backend = "mock"\n'
            'tick_s = 0.2\n'
            'reassert_s = 2.0\n'
            'min_percent = 25\n'
            'emergency_c = 90\n'
            'startup_mode = "auto"\n'
            '[curves]\n'
            'cpu = [[50, 30], [80, 100]]\n'
            'gpu = [[50, 30], [80, 100]]\n'
        )
        self._start_daemon(config_path=cfg_file)

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(str(self.socket_path))
        try:
            s.sendall(encode_request(CMD_SUBSCRIBE))
            buf = bytearray()
            frames: list[dict] = []
            while len(frames) < 2:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                while b"\n" in buf:
                    line, remainder = buf.split(b"\n", 1)
                    buf = remainder
                    if line.strip():
                        frames.append(decode_reply(line))

            self.assertGreaterEqual(len(frames), 2)
            self.assertTrue(frames[0].get("ok"))
            self.assertTrue(frames[1].get("ok"))
        finally:
            s.close()

    def test_malformed_and_oversized_frames(self) -> None:
        self._start_daemon()

        # Malformed JSON
        reply = self._send_request(b"{not valid json\n")
        self.assertFalse(reply.get("ok"))
        self.assertIn("error", reply)

        # Protocol continues functioning
        status = self._send_request(encode_request(CMD_STATUS))
        self.assertTrue(status.get("ok"))

        # Oversized frame without newline
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(str(self.socket_path))
        try:
            oversized = b"X" * (MAX_FRAME_BYTES + 1024)
            s.sendall(oversized)
            resp = s.recv(4096)
            if resp:
                obj = json.loads(resp.decode("utf-8").strip())
                self.assertFalse(obj.get("ok"))
        except OSError:
            pass
        finally:
            s.close()

        # Subsequent connection works fine
        status2 = self._send_request(encode_request(CMD_STATUS))
        self.assertTrue(status2.get("ok"))

    def test_duplicate_instance_preserves_first_socket(self) -> None:
        p1 = self._start_daemon()
        self.assertTrue(self.socket_path.exists())

        # Attempt to start instance 2 with the exact same socket and lock
        cmd2 = [
            sys.executable,
            "-c",
            ISOLATED_DAEMON,
            "--backend",
            "mock",
            "--socket",
            str(self.socket_path),
            "--lock",
            str(self.lock_path),
        ]
        res2 = subprocess.run(
            cmd2,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Instance 2 must exit non-zero
        self.assertNotEqual(res2.returncode, 0)
        self.assertIn("already running", res2.stderr + res2.stdout)

        # Instance 1 socket must still exist and respond normally
        self.assertTrue(self.socket_path.exists())
        status = self._send_request(encode_request(CMD_STATUS))
        self.assertTrue(status.get("ok"))

    def test_sigterm_and_restart_reacquisition(self) -> None:
        p1 = self._start_daemon()
        p1.terminate()
        p1.wait(timeout=3.0)
        self.assertFalse(self.socket_path.exists())

        # Restarting daemon acquires the lock and creates socket
        p2 = self._start_daemon()
        self.assertTrue(self.socket_path.exists())
        status = self._send_request(encode_request(CMD_STATUS))
        self.assertTrue(status.get("ok"))

    def test_unsupported_backend_rejected_immediately(self) -> None:
        cmd = [
            sys.executable,
            "-c",
            ISOLATED_DAEMON,
            "--backend",
            "ec",
            "--socket",
            str(self.socket_path),
            "--lock",
            str(self.lock_path),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("requires '--read-only'", res.stderr + res.stdout)
        # Did not create socket or lock
        self.assertFalse(self.socket_path.exists())

    def test_missing_explicit_config_rejected(self) -> None:
        missing_cfg = self.root / "does_not_exist.toml"
        cmd = [
            sys.executable,
            "-c",
            ISOLATED_DAEMON,
            "--config",
            str(missing_cfg),
            "--socket",
            str(self.socket_path),
            "--lock",
            str(self.lock_path),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Configuration error", res.stderr + res.stdout)

    def test_live_mock_temperatures_change_over_ticks_and_respond_to_fans(self) -> None:
        # Finding 1: temperatures change over several ticks and fan speed affects direction
        cfg_file = self.root / "live_sim.toml"
        cfg_file.write_text(
            'backend = "mock"\n'
            'tick_s = 0.1\n'
            'reassert_s = 2.0\n'
            'min_percent = 25\n'
            'emergency_c = 90\n'
            'startup_mode = "auto"\n'
            '[curves]\n'
            'cpu = [[50, 30], [80, 100]]\n'
            'gpu = [[50, 30], [80, 100]]\n'
        )
        self._start_daemon(config_path=cfg_file)

        # 1. Start in 100% max mode to drive temps cool
        self._send_request(encode_request(CMD_SET_MODE, mode="max"))
        time.sleep(0.4)
        status_cool = self._send_request(encode_request(CMD_STATUS))
        t_cool = status_cool.get("temps", {}).get("cpu")
        self.assertIsNotNone(t_cool)

        # 2. Switch to fixed 25% (low fan speed) -> temperatures must rise
        self._send_request(encode_request(CMD_SET_MODE, mode="fixed", percent=25))
        time.sleep(0.5)
        status_warm = self._send_request(encode_request(CMD_STATUS))
        t_warm = status_warm.get("temps", {}).get("cpu")
        self.assertIsNotNone(t_warm)

        # Temperatures changed and low fan speed caused warming
        self.assertGreater(t_warm, t_cool)

    def test_fragmented_and_pipelined_requests(self) -> None:
        # Finding 11: Maintain fragmented and pipelined requests
        self._start_daemon()

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(str(self.socket_path))
        try:
            # Fragmented request sent in 4-byte chunks
            req = encode_request(CMD_STATUS)
            for i in range(0, len(req), 4):
                s.sendall(req[i : i + 4])
                time.sleep(0.01)

            buf = bytearray()
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
            line, _ = buf.split(b"\n", 1)
            reply = decode_reply(line)
            self.assertTrue(reply.get("ok"))

            # Pipelined requests (two requests sent in one send)
            req2 = encode_request(CMD_STATUS) + encode_request(CMD_STATUS)
            s.sendall(req2)
            replies = []
            buf = bytearray()
            while len(replies) < 2:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                while b"\n" in buf:
                    l, remainder = buf.split(b"\n", 1)
                    buf = remainder
                    if l.strip():
                        replies.append(decode_reply(l))
            self.assertEqual(len(replies), 2)
            self.assertTrue(replies[0].get("ok"))
            self.assertTrue(replies[1].get("ok"))
        finally:
            s.close()

    def test_existing_live_endpoint_preserved_with_different_lock(self) -> None:
        # Finding 10: passing different --lock cannot unlink another live program socket
        self._start_daemon()
        self.assertTrue(self.socket_path.exists())

        other_lock = self.root / "other.lock"
        cmd = [
            sys.executable,
            "-c",
            ISOLATED_DAEMON,
            "--backend",
            "mock",
            "--socket",
            str(self.socket_path),
            "--lock",
            str(other_lock),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # Must fail and refuse to delete active socket
        self.assertNotEqual(res.returncode, 0)
        self.assertTrue(self.socket_path.exists())

        # Existing daemon is still healthy and responding
        status = self._send_request(encode_request(CMD_STATUS))
        self.assertTrue(status.get("ok"))

    def test_unsupported_backend_in_config_without_cli_flag_rejected(self) -> None:
        # Finding 12: default CLI --backend None allows nonmock backend from --config to be rejected
        cfg_file = self.root / "hwmon_config.toml"
        cfg_file.write_text(
            'backend = "hwmon"\n'
            'tick_s = 2.0\n'
            'reassert_s = 10.0\n'
            'min_percent = 25\n'
            'emergency_c = 90\n'
            'startup_mode = "auto"\n'
            '[curves]\n'
            'cpu = [[50, 30], [80, 100]]\n'
            'gpu = [[50, 30], [80, 100]]\n'
        )
        cmd = [
            sys.executable,
            "-c",
            ISOLATED_DAEMON,
            "--config",
            str(cfg_file),
            "--socket",
            str(self.socket_path),
            "--lock",
            str(self.lock_path),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not supported at Checkpoint 2", res.stderr + res.stdout)
        self.assertIn("'hwmon'", res.stderr + res.stdout)


if __name__ == "__main__":
    unittest.main()
