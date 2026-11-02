"""Socket ownership and failed-command reply regressions."""

import os
import socket
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from nitrofan.controller import Controller
from nitrofan.protocol import decode_reply, encode_request
from nitrofan.server import DaemonServer


class TestServerLifecycle(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "server.sock"
        self.controller = Mock(spec=Controller)
        self.server = DaemonServer(self.path, self.controller)
        self.addCleanup(self.server.close)

    def test_backend_failure_produces_error_reply(self):
        self.controller.apply_command.return_value = False
        self.controller.last_error = "Backend error: simulated failure"
        with patch.object(self.server, "_send_reply") as send:
            self.server._dispatch_request(Mock(), encode_request("set_mode", mode="max"))
        reply = decode_reply(send.call_args.args[1])
        self.assertFalse(reply["ok"])
        self.assertIn("simulated failure", reply["error"])

    def test_failed_bind_does_not_unlink_existing_socket(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as existing:
            existing.bind(str(self.path))
            existing.listen(1)
            with self.assertRaises(OSError):
                self.server.bind_and_listen()
            self.server.close()
            self.assertTrue(self.path.exists())

    def test_close_keeps_socket_replaced_by_another_owner(self):
        self.server.bind_and_listen()
        self.path.rename(self.path.with_name("old.sock"))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as replacement:
            replacement.bind(str(self.path))
            replacement.listen(1)
            self.server.close()
            self.assertTrue(self.path.exists())

    def test_repeated_close_does_not_close_reused_file_descriptors(self):
        self.server.close()
        with ExitStack() as stack:
            files = [stack.enter_context(open(os.devnull, "rb")) for _ in range(3)]
            self.server.close()
            for stream in files:
                os.fstat(stream.fileno())
