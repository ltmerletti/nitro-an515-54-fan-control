"""
lock.py - Single-instance locking and stale socket management.

Uses advisory file locking with fcntl.flock to guarantee that only one
instance of the daemon runs against a given socket path. Prevents race
conditions on restart and avoids unlinking sockets owned by active daemons.
"""

from __future__ import annotations

import fcntl
import os
import socket
import stat
from pathlib import Path


class LockError(Exception):
    """Raised when the daemon lock cannot be acquired."""


def default_lock_path(socket_path: str | Path) -> Path:
    """
    Compute default lock path from socket path.

    Returns /run/nitrofan/nitrofand.lock for the production socket path,
    or <socket_path>.lock for custom user/test paths. Preserves final
    path component without following symlinks.
    """
    sp = Path(os.path.abspath(socket_path))
    if str(sp) in ("/run/nitrofan/nitrofan.sock", "/var/run/nitrofan/nitrofan.sock"):
        return Path("/run/nitrofan/nitrofand.lock")
    return Path(f"{sp}.lock")


def cleanup_stale_socket(socket_path: str | Path) -> None:
    """
    Remove an existing socket file if and only if it is verified to be a stale socket.

    Must only be called after acquiring the daemon lock, ensuring that no
    active daemon owns the socket. Refuses to delete regular files, symlinks,
    or directories. Performs a bounded connect test to verify the socket is
    not active; fails closed on active endpoints or unexpected errors.
    """
    path = Path(os.path.abspath(socket_path))
    try:
        st = os.lstat(str(path))
    except FileNotFoundError:
        return
    except OSError as e:
        raise RuntimeError(f"Cannot access socket path '{path}': {e}") from e

    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(
            f"Path '{path}' is a symlink. Refusing to delete symlinks."
        )

    if not stat.S_ISSOCK(st.st_mode):
        raise RuntimeError(
            f"Path '{path}' exists and is not a socket (mode {st.st_mode:#o}). "
            "Refusing to delete non-socket file."
        )

    # Check if existing socket is active by attempting a short bounded connect
    test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    test_sock.settimeout(0.2)
    try:
        test_sock.connect(str(path))
        # Connection succeeded: active listener! Refuse to remove
        raise RuntimeError(
            f"Path '{path}' is an active, listening Unix domain socket. "
            "Refusing to delete active socket."
        )
    except ConnectionRefusedError:
        # Connection was refused; endpoint is definitively stale
        pass
    except FileNotFoundError:
        return
    except OSError as e:
        # Unknown error (e.g. permission denied) -> fail closed
        raise RuntimeError(
            f"Cannot verify socket '{path}' state ({e}). Refusing to delete."
        ) from e
    finally:
        test_sock.close()

    try:
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (st.st_dev, st.st_ino):
            raise RuntimeError(f"Socket path '{path}' changed during the stale check")
        path.unlink()
    except OSError as e:
        raise RuntimeError(f"Failed to remove stale socket '{path}': {e}") from e


class DaemonLock:
    """
    Single-instance process lock using fcntl.flock.

    The lock file is intentionally not unlinked upon release to avoid inode
    races between dying and starting processes.
    """

    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(os.path.abspath(lock_path))
        self.lock_fd: int | None = None

    def acquire(self) -> None:
        """
        Acquire non-blocking exclusive lock.

        Uses O_NOFOLLOW where supported and verifies the opened descriptor
        belongs to a regular file. Raises LockError if another process holds
        the lock or if opening fails.
        """
        if self.lock_fd is not None:
            raise LockError("This lock is already acquired")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        try:
            self.lock_fd = os.open(str(self.lock_path), flags, 0o600)
        except OSError as e:
            raise LockError(f"Cannot open lock file '{self.lock_path}': {e}") from e

        try:
            st = os.fstat(self.lock_fd)
            if not stat.S_ISREG(st.st_mode):
                raise LockError(f"Lock path '{self.lock_path}' is not a regular file")
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (LockError, OSError) as e:
            if self.lock_fd is not None:
                try:
                    os.close(self.lock_fd)
                except OSError:
                    pass
                self.lock_fd = None
            if isinstance(e, LockError):
                raise
            raise LockError(
                f"Another instance is already running (cannot acquire exclusive lock on {self.lock_path})"
            ) from e

    def release(self) -> None:
        """Release exclusive lock and close file descriptor."""
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self.lock_fd)
            except OSError:
                pass
            self.lock_fd = None

    def __enter__(self) -> DaemonLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.release()
