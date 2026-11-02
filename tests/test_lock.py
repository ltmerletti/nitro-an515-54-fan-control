"""
Tests for nitrofan.lock -- Single-instance locking and stale socket cleanup.
"""

import os
import socket
import tempfile
import unittest
from pathlib import Path

from nitrofan.lock import DaemonLock, LockError, cleanup_stale_socket, default_lock_path


class TestDaemonLock(unittest.TestCase):
    """Test process mutual exclusion and socket hygiene."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_default_lock_path(self) -> None:
        self.assertEqual(
            default_lock_path("/run/nitrofan/nitrofan.sock"),
            Path("/run/nitrofan/nitrofand.lock"),
        )
        self.assertEqual(
            default_lock_path(self.root / "test.sock"),
            self.root / "test.sock.lock",
        )

    def test_acquire_and_release(self) -> None:
        lock_file = self.root / "daemon.lock"
        lock = DaemonLock(lock_file)
        lock.acquire()
        self.assertTrue(lock_file.exists())
        lock.release()
        # Lock file must remain on disk to avoid inode races
        self.assertTrue(lock_file.exists())

    def test_second_instance_rejected(self) -> None:
        lock_file = self.root / "daemon.lock"
        lock1 = DaemonLock(lock_file)
        lock1.acquire()

        lock2 = DaemonLock(lock_file)
        with self.assertRaises(LockError):
            lock2.acquire()

        lock1.release()

        # After lock1 releases, lock2 can acquire
        lock2.acquire()
        lock2.release()

    def test_context_manager(self) -> None:
        lock_file = self.root / "daemon.lock"
        with DaemonLock(lock_file):
            lock2 = DaemonLock(lock_file)
            with self.assertRaises(LockError):
                lock2.acquire()

        # Lock was released on exit
        with DaemonLock(lock_file):
            pass

    def test_cleanup_stale_socket_success(self) -> None:
        sock_path = self.root / "stale.sock"
        # Create a real Unix domain socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(sock_path))
        s.close()
        self.assertTrue(sock_path.exists())

        cleanup_stale_socket(sock_path)
        self.assertFalse(sock_path.exists())

    def test_cleanup_stale_socket_refuses_regular_file(self) -> None:
        reg_file = self.root / "not_a_socket.sock"
        reg_file.write_text("critical data")

        with self.assertRaises(RuntimeError):
            cleanup_stale_socket(reg_file)
        self.assertTrue(reg_file.exists())

    def test_cleanup_stale_socket_refuses_directory(self) -> None:
        dir_path = self.root / "fake_socket_dir"
        dir_path.mkdir()

        with self.assertRaises(RuntimeError):
            cleanup_stale_socket(dir_path)
        self.assertTrue(dir_path.exists())

    def test_cleanup_stale_socket_missing_ok(self) -> None:
        # Non-existent path is a no-op
        cleanup_stale_socket(self.root / "nonexistent.sock")

    def test_cleanup_stale_socket_refuses_symlink_to_socket(self) -> None:
        # Finding 9: existing symlink NEVER followed or unlinked
        real_sock = self.root / "real.sock"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(real_sock))
        s.close()

        sym_sock = self.root / "sym.sock"
        sym_sock.symlink_to(real_sock)

        with self.assertRaises(RuntimeError) as ctx:
            cleanup_stale_socket(sym_sock)
        self.assertIn("symlink", str(ctx.exception).lower())
        self.assertTrue(sym_sock.is_symlink())
        self.assertTrue(real_sock.exists())

    def test_cleanup_stale_socket_refuses_active_listening_socket(self) -> None:
        # Finding 10: verify existing endpoint not active with bounded connect test before unlink
        active_sock = self.root / "active.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(active_sock))
        server.listen(1)

        try:
            with self.assertRaises(RuntimeError) as ctx:
                cleanup_stale_socket(active_sock)
            self.assertIn("active, listening", str(ctx.exception).lower())
            self.assertTrue(active_sock.exists())
        finally:
            server.close()

    def test_daemon_lock_refuses_symlink(self) -> None:
        # Finding 9: Lock open use O_NOFOLLOW, fstat regular file
        target_file = self.root / "target.txt"
        target_file.write_text("target")
        lock_symlink = self.root / "lock_symlink"
        lock_symlink.symlink_to(target_file)

        lock = DaemonLock(lock_symlink)
        with self.assertRaises(LockError):
            lock.acquire()
        self.assertTrue(target_file.exists())
        self.assertEqual(target_file.read_text(), "target")

    def test_repeated_acquire_preserves_original_lock(self) -> None:
        lock = DaemonLock(self.root / "held.lock")
        self.addCleanup(lock.release)
        lock.acquire()
        with self.assertRaises(LockError):
            lock.acquire()
        with self.assertRaises(LockError):
            DaemonLock(lock.lock_path).acquire()

    def test_non_regular_lock_releases_descriptor(self) -> None:
        path = self.root / "fifo.lock"
        os.mkfifo(path)
        lock = DaemonLock(path)
        self.addCleanup(lock.release)
        with self.assertRaises(LockError):
            lock.acquire()
        self.assertIsNone(lock.lock_fd)



if __name__ == "__main__":
    unittest.main()
