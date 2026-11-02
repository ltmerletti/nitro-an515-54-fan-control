"""
ec.py - Read-only Embedded Controller (EC) reader for Acer Nitro AN515-54.

Provides ECReader for raw, read-only single-byte reads via os.pread at offsets 0..255.
Opens /dev/ec if present, otherwise /sys/kernel/debug/ec/ec0/io, or an explicit
path for synthetic fixtures and tests.

Strictly read-only: no writable file opens, no write/pwrite calls, no hardware
control. Context manager and idempotent close ensure no leaked file descriptors.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path
from types import TracebackType

logger = logging.getLogger(__name__)

DEFAULT_DEV_EC = "/dev/ec"
DEFAULT_DEBUGFS_EC = "/sys/kernel/debug/ec/ec0/io"


class ECReader:
    """
    Read-only reader for the ACPI Embedded Controller address space (256 bytes).

    Uses os.open with O_RDONLY (and O_CLOEXEC if available) and os.pread
    for atomic single-byte reads at validated offsets 0..255.
    """

    def __init__(self, ec_path: str | Path | None = None) -> None:
        self._requested_path = str(ec_path) if ec_path is not None else None
        self._path: str | None = None
        self._fd: int | None = None

    @property
    def path(self) -> str | None:
        """Resolved path to the EC device or debugfs node."""
        return self._path

    @property
    def is_open(self) -> bool:
        """True if the reader is open and file descriptor is valid."""
        return self._fd is not None

    def _resolve_path(self) -> str:
        """
        Determine target EC path.
        If ec_path was provided in __init__, use it.
        Otherwise, prefer /dev/ec if present, then /sys/kernel/debug/ec/ec0/io.
        """
        if self._requested_path is not None:
            return self._requested_path
        if os.path.exists(DEFAULT_DEV_EC):
            return DEFAULT_DEV_EC
        return DEFAULT_DEBUGFS_EC

    def open(self) -> None:
        """
        Open the EC interface for read-only access. Idempotent.

        Raises:
            FileNotFoundError: If the EC path or kernel debugfs interface does not exist.
            PermissionError: If root/sudo permissions are required to access the interface.
            OSError: On other operating system errors.
        """
        if self._fd is not None:
            return

        target_path = self._resolve_path()
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC

        try:
            fd = os.open(target_path, flags)
        except FileNotFoundError as err:
            self._fd = None
            self._path = None
            if self._requested_path is None:
                raise FileNotFoundError(
                    f"EC interface not found: '{target_path}' does not exist (and {DEFAULT_DEV_EC} is absent). "
                    "Ensure the 'ec_sys' kernel module is loaded (e.g. 'sudo modprobe ec_sys write_support=0')."
                ) from err
            raise FileNotFoundError(f"EC path '{target_path}' not found.") from err
        except PermissionError as err:
            self._fd = None
            self._path = None
            raise PermissionError(
                f"Permission denied accessing EC at '{target_path}'. "
                "Root/sudo privileges are required to read the Embedded Controller interface."
            ) from err
        except OSError:
            self._fd = None
            self._path = None
            raise

        self._fd = fd
        self._path = target_path
        logger.debug("ECReader opened '%s' (fd=%d)", self._path, self._fd)

    def close(self) -> None:
        """Close the EC interface. Safe to call multiple times."""
        if self._fd is not None:
            fd = self._fd
            self._fd = None
            try:
                os.close(fd)
            except OSError as err:
                logger.debug("Error closing EC fd %d: %s", fd, err)

    def __enter__(self) -> ECReader:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def _validate_offset(self, offset: int) -> int:
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValueError(f"EC offset must be an integer, got {type(offset).__name__} ({offset!r})")
        if not (0 <= offset <= 255):
            raise ValueError(f"EC offset {offset:#04x} out of range 0x00..0xFF (0..255)")
        return offset

    def read_byte(self, offset: int) -> int:
        """
        Read exactly one byte from the specified offset (0..255) using os.pread.

        Raises:
            RuntimeError: If reader is not open or if a short read occurs.
            ValueError: If offset is not an integer in 0..255.
            OSError: On low-level read failure.
        """
        if self._fd is None:
            raise RuntimeError("ECReader is not open. Call open() or use as a context manager.")

        valid_offset = self._validate_offset(offset)

        try:
            data = os.pread(self._fd, 1, valid_offset)
        except OSError as err:
            raise OSError(
                f"Failed reading EC byte at offset {valid_offset:#04x} from '{self._path}': {err}"
            ) from err

        if len(data) != 1:
            raise RuntimeError(
                f"Short read at EC offset {valid_offset:#04x} from '{self._path}': "
                f"expected 1 byte, got {len(data)}"
            )

        return data[0]

    def read_u16_le(self, low_offset: int, high_offset: int, max_retries: int = 3) -> int:
        """
        Read a 16-bit little-endian unsigned integer from two EC register offsets.

        Uses high-low-high sampling with bounded retries (default 3) to mitigate
        torn reads and high-byte rollover during asynchronous firmware register updates.
        Note: this reduces rollover and torn-read risk between sequential single-byte
        reads, but cannot make firmware register updates atomic.

        Raises:
            RuntimeError: If reader is not open, if a short read occurs, or if reads
                          remain unstable across max_retries attempts.
            ValueError: If offsets are invalid or identical.
            OSError: On low-level read error.
        """
        if self._fd is None:
            raise RuntimeError("ECReader is not open. Call open() or use as a context manager.")

        valid_low = self._validate_offset(low_offset)
        valid_high = self._validate_offset(high_offset)
        if valid_low == valid_high:
            raise ValueError(
                f"low_offset and high_offset must be distinct, got {valid_low:#04x}"
            )

        attempts = max_retries if max_retries >= 1 else 1
        high1 = None
        high2 = None
        for _ in range(attempts):
            high1 = self.read_byte(valid_high)
            low = self.read_byte(valid_low)
            high2 = self.read_byte(valid_high)
            if high1 == high2:
                return low | (high2 << 8)

        raise RuntimeError(
            f"Unstable EC 16-bit read at low={valid_low:#04x}, high={valid_high:#04x}: "
            f"high byte changed ({high1:#02x} != {high2:#02x}) across {attempts} retries"
        )

    def read_registers(self, offsets: Iterable[int]) -> dict[int, int]:
        """
        Read multiple registers individually and return a mapping of offset -> byte value.

        Does not invent default or zero values on read error. Any failure propagates immediately.
        """
        results: dict[int, int] = {}
        for off in offsets:
            valid_off = self._validate_offset(off)
            results[valid_off] = self.read_byte(valid_off)
        return results

    def read_all(self) -> dict[int, int]:
        """Read all 256 bytes (0x00..0xFF) of EC space individually."""
        return self.read_registers(range(256))
