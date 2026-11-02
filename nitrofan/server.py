"""
server.py - Unix domain socket server and IPC dispatcher for NitroFan.

Handles JSON-lines protocol framing, non-blocking selector-based event loop,
client subscriptions, bounded output buffers with EVENT_WRITE, and atomic
controller command dispatch.
"""

from __future__ import annotations

import logging
import os
import selectors
import socket
import stat
from pathlib import Path
from typing import Any

from nitrofan.config import ConfigError
from nitrofan.controller import Controller
from nitrofan.protocol import (
    CMD_SAVE,
    CMD_SET_CURVE,
    CMD_SET_MODE,
    CMD_STATUS,
    CMD_SUBSCRIBE,
    MAX_FRAME_BYTES,
    ProtocolError,
    decode_request,
    encode_error,
    encode_reply,
)

logger = logging.getLogger(__name__)

MAX_CLIENTS = 32
MAX_CLIENT_OUT_BUFFER = 256 * 1024


class DaemonServer:
    """
    Unix socket server running a single-threaded non-blocking selector loop.

    Ensures controller interactions are fully serialized with periodic ticks,
    preventing race conditions between client commands and safety updates.
    """

    def __init__(
        self,
        socket_path: str | Path,
        controller: Controller,
        signal_pipe: tuple[int, int] | None = None,
    ) -> None:
        self.socket_path = Path(os.path.abspath(socket_path))
        self.controller = controller
        self.selector = selectors.DefaultSelector()
        self.server_sock: socket.socket | None = None
        self.subscribers: set[socket.socket] = set()
        self.client_in_buffers: dict[socket.socket, bytearray] = {}
        self.client_out_buffers: dict[socket.socket, bytearray] = {}
        self.running: bool = False
        self._closed = False

        self._bound_inode: int | None = None
        self._bound_dev: int | None = None

        # Self-pipe for asynchronous signal wakeup
        if signal_pipe is not None:
            self._r_pipe, self._w_pipe = signal_pipe
            self._owns_signal_pipe = False
        else:
            self._r_pipe, self._w_pipe = os.pipe()
            os.set_blocking(self._r_pipe, False)
            os.set_blocking(self._w_pipe, False)
            self._owns_signal_pipe = True

    def bind_and_listen(self) -> None:
        """
        Create, bind, chmod 0660, and listen on the Unix domain socket.

        Fails closed if chmod 0660 fails. Tracks bound inode for safe cleanup.
        """
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_sock.setblocking(False)
        self.server_sock.bind(str(self.socket_path))

        try:
            st = os.lstat(str(self.socket_path))
            self._bound_inode = st.st_ino
            self._bound_dev = st.st_dev
        except OSError as e:
            self.server_sock.close()
            self.server_sock = None
            raise RuntimeError(f"Failed to stat freshly bound socket '{self.socket_path}': {e}") from e

        # Permissions 0660: accessible by owner and group (fail closed)
        try:
            os.chmod(str(self.socket_path), 0o660)
        except OSError as e:
            logger.error("Could not set 0660 permissions on socket '%s': %s", self.socket_path, e)
            try:
                self.server_sock.close()
            except Exception:
                pass
            self.server_sock = None
            self._safe_unlink_bound_socket()
            raise RuntimeError(f"Failed to set 0660 permissions on socket: {e}") from e

        self.server_sock.listen(16)
        self.selector.register(self.server_sock, selectors.EVENT_READ, data=self._on_accept)
        self.selector.register(self._r_pipe, selectors.EVENT_READ, data=self._on_signal_pipe)
        logger.info("Listening on Unix socket: %s", self.socket_path)

    def _safe_unlink_bound_socket(self) -> None:
        """Unlink the socket only if we own the bound inode."""
        if self._bound_inode is None:
            return
        try:
            curr_st = os.lstat(str(self.socket_path))
            if curr_st.st_ino == self._bound_inode and curr_st.st_dev == self._bound_dev:
                os.unlink(str(self.socket_path))
        except OSError:
            pass
        self._bound_inode = None
        self._bound_dev = None

    def _on_signal_pipe(self, key: selectors.SelectorKey, mask: int) -> None:
        """Signal pipe became readable; drain and stop loop."""
        try:
            os.read(self._r_pipe, 1024)
        except OSError:
            pass
        self.running = False

    def _on_accept(self, key: selectors.SelectorKey, mask: int) -> None:
        """Accept a new client connection."""
        if self.server_sock is None:
            return
        try:
            client, _ = self.server_sock.accept()
        except OSError:
            return

        if len(self.client_in_buffers) >= MAX_CLIENTS:
            logger.warning("Max client limit (%d) reached; rejecting connection", MAX_CLIENTS)
            try:
                client.close()
            except OSError:
                pass
            return

        client.setblocking(False)
        self.client_in_buffers[client] = bytearray()
        self.client_out_buffers[client] = bytearray()
        self.selector.register(client, selectors.EVENT_READ, data=self._on_client_event)
        logger.debug("Client connected (fd=%d)", client.fileno())

    def _close_client(self, client: socket.socket) -> None:
        """Clean up and close a client connection."""
        self.subscribers.discard(client)
        self.client_in_buffers.pop(client, None)
        self.client_out_buffers.pop(client, None)
        try:
            self.selector.unregister(client)
        except Exception:
            pass
        try:
            client.close()
        except OSError:
            pass

    def _on_client_event(self, key: selectors.SelectorKey, mask: int) -> None:
        """Handle readable or writable state on a client socket."""
        client = key.fileobj
        assert isinstance(client, socket.socket)
        if mask & selectors.EVENT_WRITE:
            self._on_client_write(client)
        if (mask & selectors.EVENT_READ) and client in self.client_in_buffers:
            self._on_client_read(client)

    def _on_client_write(self, client: socket.socket) -> None:
        """Flush queued output buffer to client socket."""
        out_buf = self.client_out_buffers.get(client)
        if out_buf is None:
            return
        try:
            sent = client.send(out_buf)
            del out_buf[:sent]
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._close_client(client)
            return

        if not out_buf:
            try:
                self.selector.modify(client, selectors.EVENT_READ, data=self._on_client_event)
            except Exception:
                pass

    def _on_client_read(self, client: socket.socket) -> None:
        """Read incoming data from a client socket and dispatch framed requests."""
        in_buf = self.client_in_buffers.get(client)
        if in_buf is None:
            return

        try:
            chunk = client.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._close_client(client)
            return

        if not chunk:
            self._close_client(client)
            return

        in_buf.extend(chunk)

        # Process frames per newline, checking MAX_FRAME_BYTES per frame
        while b"\n" in in_buf:
            if client not in self.client_in_buffers:
                return
            newline_idx = in_buf.index(b"\n")
            line = bytes(in_buf[: newline_idx + 1])
            del in_buf[: newline_idx + 1]

            if len(line) > MAX_FRAME_BYTES:
                self._send_reply(client, encode_error(f"frame too large: exceeds {MAX_FRAME_BYTES} limit"))
                self._close_client(client)
                return

            stripped = line.strip()
            if not stripped:
                continue

            self._dispatch_request(client, line)

        if len(in_buf) > MAX_FRAME_BYTES:
            self._send_reply(client, encode_error(f"frame too large: exceeds {MAX_FRAME_BYTES} limit"))
            self._close_client(client)
            return

    def _dispatch_request(self, client: socket.socket, line: bytes) -> None:
        """Parse and execute a single JSON-lines command."""
        try:
            cmd, req = decode_request(line)
        except ProtocolError as e:
            self._send_reply(client, encode_error(str(e)))
            return

        try:
            if cmd == CMD_STATUS:
                status = self.controller.get_status()
                payload = {k: v for k, v in status.items() if k != "ok"}
                self._send_reply(client, encode_reply(True, **payload))

            elif cmd == CMD_SET_MODE:
                mode = req.get("mode")
                if not isinstance(mode, str):
                    self._send_reply(client, encode_error("set_mode requires string 'mode'"))
                    return
                percent = req.get("percent")
                percents = req.get("percents")
                self.controller.set_mode(mode, percent=percent, percents=percents)
                applied = self.controller.apply_command()
                self._send_reply(client, encode_reply(True) if applied else
                                 encode_error(self.controller.last_error or "Mode change failed; returned to auto"))

            elif cmd == CMD_SET_CURVE:
                fan = req.get("fan")
                points = req.get("points")
                if not isinstance(fan, str):
                    self._send_reply(client, encode_error("set_curve requires string 'fan'"))
                    return
                if not isinstance(points, list):
                    self._send_reply(client, encode_error("set_curve requires list 'points'"))
                    return
                self.controller.set_curve(fan, points)
                applied = self.controller.apply_command()
                self._send_reply(client, encode_reply(True) if applied else
                                 encode_error(self.controller.last_error or "Curve application failed; returned to auto"))

            elif cmd == CMD_SAVE:
                self._send_reply(
                    client,
                    encode_error("save is not implemented at Checkpoint 2 (persistence belongs to later milestone)")
                )

            elif cmd == CMD_SUBSCRIBE:
                self.subscribers.add(client)
                status = self.controller.get_status()
                payload = {k: v for k, v in status.items() if k != "ok"}
                self._send_reply(client, encode_reply(True, **payload))

            else:
                self._send_reply(client, encode_error(f"unknown command '{cmd}'"))

        except (ValueError, ConfigError) as e:
            self._send_reply(client, encode_error(str(e)))
        except Exception as e:
            logger.exception("Unexpected error processing request '%s': %s", cmd, e)
            self._send_reply(client, encode_error(f"internal error: {e}"))

    def _send_reply(self, client: socket.socket, data: bytes) -> None:
        """Queue reply data to client output buffer and flush what fits."""
        out_buf = self.client_out_buffers.get(client)
        if out_buf is None:
            return

        out_buf.extend(data)
        if len(out_buf) > MAX_CLIENT_OUT_BUFFER:
            logger.warning("Client output buffer exceeded %d bytes; disconnecting slow client", MAX_CLIENT_OUT_BUFFER)
            self._close_client(client)
            return

        try:
            sent = client.send(out_buf)
            del out_buf[:sent]
        except (BlockingIOError, InterruptedError):
            sent = 0
        except OSError:
            self._close_client(client)
            return

        if out_buf:
            try:
                self.selector.modify(client, selectors.EVENT_READ | selectors.EVENT_WRITE, data=self._on_client_event)
            except Exception:
                pass
        else:
            try:
                self.selector.modify(client, selectors.EVENT_READ, data=self._on_client_event)
            except Exception:
                pass

    def _broadcast_status(self) -> None:
        """Stream current status to all subscribed clients."""
        if not self.subscribers:
            return
        status = self.controller.get_status()
        payload = {k: v for k, v in status.items() if k != "ok"}
        data = encode_reply(True, **payload)

        for client in list(self.subscribers):
            self._send_reply(client, data)

    def run_loop(self) -> None:
        """
        Main execution loop. Alternates between selector events and controller ticks.
        """
        self.running = True
        tick_interval = self.controller.config.tick_s
        next_tick = self.controller.clock()

        # Initial tick on startup
        self.controller.tick()
        next_tick += tick_interval

        while self.running:
            now = self.controller.clock()
            if now >= next_tick:
                self.controller.tick()
                self._broadcast_status()
                next_tick = now + tick_interval

            wait_time = max(0.0, next_tick - self.controller.clock())
            timeout = min(wait_time, 0.5)

            try:
                events = self.selector.select(timeout=timeout)
            except InterruptedError:
                continue

            for key, mask in events:
                callback = key.data
                callback(key, mask)

    def signal_stop(self) -> None:
        """Trigger graceful shutdown from a signal handler."""
        try:
            os.write(self._w_pipe, b"\x00")
        except OSError:
            pass

    def close(self) -> None:
        """
        Close sockets and remove socket file if owned.
        """
        if self._closed:
            return
        self._closed = True
        for client in list(self.client_in_buffers.keys()):
            self._close_client(client)

        if self.server_sock is not None:
            try:
                self.selector.unregister(self.server_sock)
            except Exception:
                pass
            try:
                self.server_sock.close()
            except OSError:
                pass
            self.server_sock = None

        try:
            self.selector.unregister(self._r_pipe)
        except Exception:
            pass

        if self._owns_signal_pipe:
            for fd in (self._r_pipe, self._w_pipe):
                try:
                    os.close(fd)
                except OSError:
                    pass

        try:
            self.selector.close()
        except Exception:
            pass

        self._safe_unlink_bound_socket()
