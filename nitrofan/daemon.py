"""
daemon.py - NitroFan daemon entry point and process manager.

Runs the fan control service, binds the Unix domain socket, manages single
instance file locking, handles POSIX signals, and ensures fail-safe cleanup.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import signal
import sys
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any

from nitrofan.backends.base import Backend
from nitrofan.backends.ec import ECBackend
from nitrofan.backends.hwmon import HwmonBackend
from nitrofan.backends.mock import MockBackend, MockSensorProvider, SimulatedSensorProvider
from nitrofan.backends.nbfc import NBFCBackend
from nitrofan.sensors import SystemSensorProvider
from nitrofan.config import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    CurvePoint,
    CurvesConfig,
    load_config,
)
from nitrofan.controller import Controller
from nitrofan.lock import DaemonLock, LockError, cleanup_stale_socket, default_lock_path
from nitrofan.protocol import DEFAULT_SOCKET_PATH
from nitrofan.server import DaemonServer

logger = logging.getLogger("nitrofan.daemon")


def get_default_mock_config() -> Config:
    """
    Produce a safe default configuration for the mock backend when running
    without an explicit configuration file.
    """
    return Config(
        backend="mock",
        tick_s=2.0,
        reassert_s=10.0,
        min_percent=25,
        emergency_c=90,
        startup_mode="auto",
        ec=None,
        curves=CurvesConfig(
            fans=MappingProxyType({
                "cpu": (
                    CurvePoint(45.0, 25),
                    CurvePoint(60.0, 40),
                    CurvePoint(70.0, 60),
                    CurvePoint(80.0, 85),
                    CurvePoint(88.0, 100),
                ),
                "gpu": (
                    CurvePoint(45.0, 25),
                    CurvePoint(60.0, 40),
                    CurvePoint(70.0, 60),
                    CurvePoint(78.0, 85),
                    CurvePoint(85.0, 100),
                ),
            })
        ),
    )


def run_daemon(
    socket_path: str | Path,
    config_path: str | Path | None = None,
    backend_name: str | None = None,
    read_only: bool = False,
    sysfs_root: str | Path = "",
    lock_path: str | Path | None = None,
    ec_path: str | Path | None = None,
    synthetic_root: str | Path | None = None,
) -> int:
    """
    Initialize and run the daemon service.

    Returns exit status code (0 on clean shutdown, non-zero on error).
    """
    # Early check on explicit CLI backend
    cli_backend = backend_name.lower() if backend_name else None
    if cli_backend == "ec" and not read_only:
        sys.stderr.write(
            "Error: Backend 'ec' requires '--read-only'. "
            "Phase 0 hardware control verification is incomplete; fan control is not supported.\n"
        )
        return 1

    # Resolve configuration
    config: Config
    if config_path is not None:
        try:
            config = load_config(config_path)
        except (ConfigError, FileNotFoundError, OSError) as e:
            sys.stderr.write(f"Configuration error: {e}\n")
            return 1
    else:
        # Check standard path
        if Path(DEFAULT_CONFIG_PATH).exists():
            try:
                config = load_config(DEFAULT_CONFIG_PATH)
            except (ConfigError, OSError) as e:
                sys.stderr.write(f"Configuration error in {DEFAULT_CONFIG_PATH}: {e}\n")
                return 1
        else:
            # Use safe built-in mock defaults
            config = get_default_mock_config()

    effective_backend = cli_backend or config.backend
    if effective_backend == "ec":
        if not read_only:
            sys.stderr.write(
                "Error: Backend 'ec' requires '--read-only'. "
                "Phase 0 hardware control verification is incomplete; fan control is not supported.\n"
            )
            return 1
        if not config.ec_rpm or not len(config.ec_rpm):
            sys.stderr.write(
                "Error: Backend 'ec' requires configured [ec_rpm] section with fan RPM mappings.\n"
            )
            return 1
        if config.startup_mode != "auto":
            sys.stderr.write(
                f"Error: Read-only backend requires startup_mode 'auto', got '{config.startup_mode}'. "
                "Fan control is not supported.\n"
            )
            return 1
    elif effective_backend == "hwmon":
        if not read_only:
            sys.stderr.write(
                f"Error: Backend '{effective_backend}' requires '--read-only' (not supported at Checkpoint 2 without '--read-only'). "
                "Phase 0 hardware control verification is incomplete; fan control is not supported.\n"
            )
            return 1
        if config.startup_mode != "auto":
            sys.stderr.write(
                f"Error: Read-only backend requires startup_mode 'auto', got '{config.startup_mode}'. "
                "Fan control is not supported.\n"
            )
            return 1
    elif effective_backend == "nbfc":
        if read_only:
            sys.stderr.write("Error: use the hwmon backend for read-only monitoring.\n")
            return 1
        if config.nbfc is None:
            sys.stderr.write("Error: nbfc requires an explicit NBFC configuration.\n")
            return 1
        if config.startup_mode != "auto":
            sys.stderr.write(
                f"Error: Backend 'nbfc' requires startup_mode 'auto', got '{config.startup_mode}'.\n"
            )
            return 1
    elif effective_backend != "mock":
        sys.stderr.write(
            f"Error: Backend '{effective_backend}' is not supported.\n"
        )
        return 1

    # Install signal handlers BEFORE backend open or file operations
    r_pipe, w_pipe = os.pipe()
    os.set_blocking(r_pipe, False)
    os.set_blocking(w_pipe, False)
    stop_requested = False

    def _on_signal(signum: int, frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        try:
            os.write(w_pipe, b"\x00")
        except OSError:
            pass

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # Resolve paths (preserving final component without following symlinks)
    socket_p = Path(os.path.abspath(socket_path))
    lock_p = Path(os.path.abspath(lock_path)) if lock_path else default_lock_path(socket_p)

    daemon_lock = DaemonLock(lock_p)
    server: DaemonServer | None = None
    backend: Backend | None = None
    cleaned_up = False

    def do_cleanup() -> None:
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        logger.info("Cleaning up daemon resources...")
        if server is not None:
            try:
                server.close()
            except Exception as e:
                logger.error("Error closing server: %s", e)
        # Ensure backend cleanup happens BEFORE releasing lock
        if backend is not None:
            if not getattr(backend, "read_only", False):
                try:
                    backend.set_auto()
                except Exception as e:
                    logger.error("Error setting auto on backend during cleanup: %s", e)
            try:
                backend.close()
            except Exception as e:
                logger.error("Error closing backend: %s", e)
        try:
            daemon_lock.release()
        except Exception as e:
            logger.error("Error releasing daemon lock: %s", e)
        for fd in (r_pipe, w_pipe):
            try:
                os.close(fd)
            except OSError:
                pass

    atexit.register(do_cleanup)

    try:
        if stop_requested:
            return 0

        # Acquire single instance lock
        try:
            daemon_lock.acquire()
        except LockError as e:
            sys.stderr.write(f"Lock error: {e}\n")
            return 1

        if stop_requested:
            return 0

        # Clean up stale socket from a previous crashed run (only after lock held)
        try:
            cleanup_stale_socket(socket_p)
        except RuntimeError as e:
            sys.stderr.write(f"Socket path error: {e}\n")
            return 1

        if stop_requested:
            return 0

        # Initialize backend and sensors
        if effective_backend == "hwmon":
            backend = HwmonBackend(sysfs_root=sysfs_root)
            try:
                backend.open()
            except Exception as e:
                sys.stderr.write(f"Backend error: {e}\n")
                return 1
            sensor_provider = SystemSensorProvider(sysfs_root=sysfs_root)
        elif effective_backend == "ec":
            assert config.ec_rpm is not None
            backend = ECBackend(ec_rpm=config.ec_rpm, ec_path=ec_path)
            try:
                backend.open()
            except Exception as e:
                sys.stderr.write(f"Backend error: {e}\n")
                return 1
            sensor_provider = SystemSensorProvider(sysfs_root=sysfs_root)
        elif effective_backend == "nbfc":
            assert config.nbfc is not None
            backend = NBFCBackend(
                config=config.nbfc,
                sysfs_root=sysfs_root,
                ec_path=ec_path,
                synthetic_root=synthetic_root,
            )
            try:
                backend.open()
            except Exception as e:
                sys.stderr.write(f"Backend error: {e}\n")
                return 1
            sensor_root = backend.sysfs_root or (
                backend.synthetic_root / 'sys' if backend.synthetic_root else None)
            if sensor_root is None:
                sensor_provider = SystemSensorProvider()
            else:
                hwmon_root = sensor_root / 'class/hwmon'
                if not hwmon_root.exists():
                    hwmon_root = sensor_root / 'sys/class/hwmon'
                sensor_provider = SystemSensorProvider(hwmon_root=hwmon_root,
                                                       pci_root=sensor_root / 'bus/pci/devices')
        else:
            backend = MockBackend(read_only=read_only)
            backend.open()
            mock_sensors = MockSensorProvider()
            sensor_provider = SimulatedSensorProvider(
                provider=mock_sensors,
                backend=backend,
                clock=time.monotonic,
            )

        # Initialize controller with monotonic clock and wall clock for sleep/resume gap
        controller = Controller(
            config=config,
            backend=backend,
            sensor_provider=sensor_provider,
            clock=time.monotonic,
            wall_clock=time.time,
        )

        # Initialize server sharing the signal wakeup pipe
        server = DaemonServer(socket_p, controller, signal_pipe=(r_pipe, w_pipe))
        server.bind_and_listen()

        if stop_requested:
            return 0

        logger.info(
            "NitroFan daemon running (backend=%s, read_only=%s, socket=%s)",
            effective_backend,
            getattr(backend, "read_only", False),
            socket_p,
        )
        server.run_loop()
        return 0

    except Exception as e:
        logger.exception("Fatal error in daemon execution: %s", e)
        return 1
    finally:
        do_cleanup()
        try:
            atexit.unregister(do_cleanup)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NitroFan daemon - Acer Nitro fan control service"
    )
    parser.add_argument(
        "--backend",
        choices=["mock", "ec", "hwmon", "nbfc"],
        default=None,
        help="Fan control backend (default: None, inferred from config or 'mock')",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Run in read-only monitoring mode (required for hwmon and ec; no hardware writes)",
    )
    parser.add_argument(
        "--sysfs-root",
        default="",
        help="Root to prepend for sysfs paths (useful for testing with fake sysfs tree)",
    )
    parser.add_argument(
        "--synthetic-root",
        default=None,
        help="Root for synthetic test sandbox",
    )
    parser.add_argument(
        "--ec-path",
        default=None,
        help="Path to EC interface (for fixtures and tests)",
    )
    parser.add_argument(
        "--socket",
        default=DEFAULT_SOCKET_PATH,
        help=f"Unix domain socket path (default: {DEFAULT_SOCKET_PATH})",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to TOML configuration file",
    )
    parser.add_argument(
        "--lock",
        default=None,
        help="Path to single-instance lock file",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging output",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    exit_code = run_daemon(
        socket_path=args.socket,
        config_path=args.config,
        backend_name=args.backend,
        read_only=args.read_only,
        sysfs_root=args.sysfs_root,
        lock_path=args.lock,
        ec_path=args.ec_path,
        synthetic_root=args.synthetic_root,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
