"""
nbfc.py - NBFC guarded hardware backend for Acer Nitro AN515-54.

Interfaces with the independent guardian process to manage fan speeds via
upstream NBFC and the ec_guard safety filter.
Enforces neutral fan names ('fan1', 'fan2'), equal combined fan speeds (25..100%),
firmware Auto on startup, and atomic rejection of software curves or unequal targets.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import select
import sys
import time
from typing import Any, Mapping

from nitrofan.backends.base import Backend
from nitrofan.config import NBFCConfig

logger = logging.getLogger("nitrofan.backends.nbfc")


class NBFCBackend(Backend):
    """
    Fan control backend communicating with the NBFC guardian supervisor.
    """

    fans: list[str] = ["fan1", "fan2"]
    name: str = "nbfc"
    read_only: bool = False
    can_control: bool = True
    can_curve: bool = False

    def __init__(
        self,
        config: NBFCConfig | None = None,
        sysfs_root: str | Path | None = None,
        ec_path: str | Path | None = None,
        synthetic_root: str | Path | None = None,
        runtime_dir: str | Path | None = None,
        manifest_path: str | Path | None = None,
        policy_path: str | Path | None = None,
        profile_path: str | Path | None = None,
        service_bin: str | Path | None = None,
    ) -> None:
        self.config = config or NBFCConfig()

        self.runtime_dir = Path(
            runtime_dir or self.config.runtime_dir
        ).resolve()
        self.manifest_path = Path(
            manifest_path or self.config.manifest_path
        ).resolve()
        self.policy_path = Path(
            policy_path or self.config.policy_path
        ).resolve() if (policy_path or self.config.policy_path) else None
        self.profile_path = Path(
            profile_path or self.config.profile_path
        ).resolve() if (profile_path or self.config.profile_path) else None
        self.service_bin = Path(
            service_bin or self.config.service_bin
        ).resolve() if (service_bin or self.config.service_bin) else None

        self.sysfs_root = Path(
            sysfs_root or self.config.sysfs_root
        ).resolve() if (sysfs_root or self.config.sysfs_root) else None
        self.ec_path = Path(
            ec_path or self.config.ec_path
        ).resolve() if (ec_path or self.config.ec_path) else None
        self.synthetic_root = Path(
            synthetic_root or self.config.synthetic_root
        ).resolve() if (synthetic_root or self.config.synthetic_root) else None

        self._guardian_proc: subprocess.Popen | None = None
        self._current_percent: int | None = None
        self._rpm_cache: dict[str, int | None] = {"fan1": None, "fan2": None}
        self._is_open: bool = False
        self.emergency = False
        self.safety_status: dict[str, Any] = {}
        self._reply_buffer = bytearray()

    def open(self) -> None:
        """Spawn the independent guardian supervisor process and establish pipe IPC."""
        if self._is_open:
            return

        cmd = [
            sys.executable,
            "-m",
            "nitrofan.nbfc_guardian",
            "--guardian",
            "--runtime-dir",
            str(self.runtime_dir),
            "--manifest",
            str(self.manifest_path),
        ]
        if self.policy_path is not None:
            cmd.extend(["--policy", str(self.policy_path)])
        if self.profile_path is not None:
            cmd.extend(["--profile", str(self.profile_path)])
        if self.service_bin is not None:
            cmd.extend(["--service-bin", str(self.service_bin)])
        if self.ec_path is not None:
            cmd.extend(["--ec-path", str(self.ec_path)])
        if self.sysfs_root is not None:
            cmd.extend(["--sysfs-root", str(self.sysfs_root)])
        if self.synthetic_root is not None:
            cmd.extend(["--synthetic-root", str(self.synthetic_root)])

        self._guardian_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
            start_new_session=True,
            cwd=Path(__file__).resolve().parents[2],
        )

        # Wait for ready signal from guardian
        assert self._guardian_proc.stdout is not None
        try:
            ready_data = self._read_reply()
            if not ready_data.get("ready"):
                raise RuntimeError(f"Unexpected guardian ready response: {ready_data}")
        except Exception:
            self.close()
            raise

        self._is_open = True
        self._current_percent = None

    def _read_reply(self) -> dict[str, Any]:
        assert self._guardian_proc and self._guardian_proc.stdout
        deadline = time.monotonic() + 15.0
        while b'\n' not in self._reply_buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self._guardian_proc.stdout], [], [], remaining)[0]:
                raise RuntimeError("Guardian response timed out")
            chunk = os.read(self._guardian_proc.stdout.fileno(), 4096)
            if not chunk:
                raise RuntimeError("Guardian closed its response pipe")
            self._reply_buffer.extend(chunk)
            if len(self._reply_buffer) > 4096:
                raise RuntimeError("Guardian response exceeded its frame limit")
        line, _, remainder = self._reply_buffer.partition(b'\n')
        self._reply_buffer[:] = remainder
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise RuntimeError("Invalid guardian response")
        return reply

    def _send_command(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON command frame to guardian and receive reply frame."""
        if not self._is_open or self._guardian_proc is None or self._guardian_proc.stdin is None:
            raise RuntimeError("Backend is not open")

        msg = json.dumps(cmd).encode("utf-8") + b"\n"
        try:
            self._guardian_proc.stdin.write(msg)
            self._guardian_proc.stdin.flush()
        except OSError as e:
            raise RuntimeError(f"Failed to communicate with guardian: {e}") from e

        return self._read_reply()

    def set_auto(self) -> None:
        """Return all fans to firmware automatic control."""
        if not self._is_open:
            return
        try:
            reply = self._send_command({"cmd": "set_auto"})
        except Exception:
            self.close()
            return
        if not reply.get("ok"):
            raise RuntimeError(f"Failed to set_auto on guardian: {reply.get('error')}")
        self._current_percent = None

    def set_targets(self, targets: Mapping[str, int]) -> None:
        """
        Set target fan speeds across both fans.
        Both fan1 and fan2 must have equal speed between 25 and 100%.
        """
        if not self._is_open:
            raise RuntimeError("Backend is not open")

        if set(targets) != set(self.fans):
            raise ValueError("Both fan1 and fan2 targets are required")
        for fan in targets:
            if fan not in self.fans:
                raise ValueError(f"Unknown fan '{fan}'; available: {self.fans}")

        speeds = list(targets.values())
        if len(set(speeds)) > 1:
            raise ValueError("nbfc backend requires equal combined fan speeds")

        pct = speeds[0]
        if isinstance(pct, bool) or not isinstance(pct, int):
            raise ValueError(f"Fan speed must be integer percentage, got {type(pct).__name__}")

        if not (25 <= pct <= 100):
            raise ValueError(f"nbfc backend requires fixed percent in range 25..100, got {pct}")

        reply = self._send_command({"cmd": "set_targets", "percent": pct})
        if not reply.get("ok"):
            raise RuntimeError(f"Guardian failed to set target {pct}%: {reply.get('error')}")

        self._current_percent = pct

    def set_percent(self, fan: str, pct: int) -> None:
        """Set speed percent for a fan (sets both fans together to maintain symmetry)."""
        if fan not in self.fans:
            raise ValueError(f"Unknown fan '{fan}'; available: {self.fans}")
        self.set_targets({f: pct for f in self.fans})

    def read_percent(self, fan: str) -> int | None:
        """Return the requested target percentage for the fan (or None in auto mode)."""
        if fan not in self.fans:
            raise ValueError(f"Unknown fan '{fan}'; available: {self.fans}")
        self.check_health()
        return self._current_percent

    def check_health(self) -> None:
        if not self._is_open:
            self.open()
        status = self._send_command({'cmd': 'get_status'})
        if status.get('ok') is not True or status.get('fault'):
            raise RuntimeError(status.get('fault') or 'Guardian status is unavailable')
        self._current_percent = status.get('percent')
        self.emergency = bool(status.get('emergency'))
        self.safety_status = status.get('safety', {})
        self._rpm_cache = status.get('rpms', {})

    def read_rpm(self, fan: str) -> int | None:
        """Return the latest tachometer RPM reading for the specified fan."""
        if fan not in self.fans:
            raise ValueError(f"Unknown fan '{fan}'; available: {self.fans}")
        if not self._is_open:
            return None
        self.check_health()
        return self._rpm_cache.get(fan)

    def close(self) -> None:
        """Idempotently close connection to guardian and restore firmware Auto."""
        if self._guardian_proc is None:
            return
        process = self._guardian_proc
        try:
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
            from nitrofan.nbfc_guardian import run_recovery
            if run_recovery(runtime_dir=self.runtime_dir, manifest_path=self.manifest_path,
                            policy_path=self.policy_path, synthetic_root=self.synthetic_root,
                            ec_path=self.ec_path, sysfs_root=self.sysfs_root):
                raise RuntimeError("Firmware recovery failed; the session record was retained")
        finally:
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
            self._guardian_proc = None
            self._is_open = False
            self._current_percent = None
            self._rpm_cache = {fan: None for fan in self.fans}
            self._reply_buffer.clear()
