"""
protocol.py - JSON-lines framing for NitroFan daemon/client IPC.

Every message is one JSON object per line, UTF-8 encoded. Every reply
includes "ok": true or "ok": false with an "error" string on failure.

This module provides encode/decode helpers, frame size limits, strict
JSON validation (rejects NaN/Infinity), and command/reply constants.
No network server, no socket binding -- just wire format helpers.
"""

from __future__ import annotations

import json
from typing import Any

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------

DEFAULT_SOCKET_PATH = "/run/nitrofan/nitrofan.sock"

# Max bytes for a single JSON-lines frame. 64 KiB is generous for
# status responses with full curve data but blocks abuse.
MAX_FRAME_BYTES = 64 * 1024

# Command names (not implemented yet, but defined for forward reference)
CMD_STATUS = "status"
CMD_SET_MODE = "set_mode"
CMD_SET_CURVE = "set_curve"
CMD_SAVE = "save"
CMD_SUBSCRIBE = "subscribe"

KNOWN_COMMANDS = (CMD_STATUS, CMD_SET_MODE, CMD_SET_CURVE, CMD_SAVE, CMD_SUBSCRIBE)

# Mode values for set_mode
MODE_AUTO = "auto"
MODE_MAX = "max"
MODE_FIXED = "fixed"
MODE_CURVE = "curve"

KNOWN_MODES = (MODE_AUTO, MODE_MAX, MODE_FIXED, MODE_CURVE)


class ProtocolError(Exception):
    """Raised on malformed, oversized, or invalid protocol messages."""


# -----------------------------------------------------------------------
# Encoding
# -----------------------------------------------------------------------

def encode_request(cmd: str, **params: Any) -> bytes:
    """
    Encode a client request as a JSON-lines frame (one line, UTF-8, newline terminated).

    Raises ProtocolError if the result exceeds MAX_FRAME_BYTES.
    """
    obj: dict[str, Any] = {"cmd": cmd}
    obj.update(params)
    return _encode_obj(obj)


def encode_reply(ok: bool, **fields: Any) -> bytes:
    """
    Encode a daemon reply. ok=True for success, ok=False with error=<str> for failure.

    Raises ProtocolError if the result exceeds MAX_FRAME_BYTES.
    """
    obj: dict[str, Any] = {"ok": ok}
    obj.update(fields)
    _validate_reply(obj)
    return _encode_obj(obj)


def encode_error(message: str) -> bytes:
    """Shorthand for encode_reply(ok=False, error=message)."""
    return encode_reply(False, error=message)


def _encode_obj(obj: dict[str, Any]) -> bytes:
    """Serialize a dict to a JSON line with strict float handling."""
    try:
        # allow_nan=False rejects NaN/Infinity in the data
        line = json.dumps(obj, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError) as e:
        raise ProtocolError(f"cannot encode: {e}") from e
    raw = (line + "\n").encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"frame too large: {len(raw)} bytes > {MAX_FRAME_BYTES} limit"
        )
    return raw


# -----------------------------------------------------------------------
# Decoding
# -----------------------------------------------------------------------

def decode_line(raw: bytes) -> dict[str, Any]:
    """
    Decode one JSON-lines frame from raw bytes.

    The input should be a single line (with or without trailing newline).
    Validates: size limit, valid JSON, top-level must be an object, no
    NaN/Infinity floats.

    Returns the parsed dict.
    Raises ProtocolError on any validation failure.
    """
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"frame too large: {len(raw)} bytes > {MAX_FRAME_BYTES} limit"
        )
    content = raw.removesuffix(b"\n").removesuffix(b"\r")
    if b"\n" in content or b"\r" in content:
        raise ProtocolError("frame must contain exactly one JSON line")
    stripped = content.strip()
    if not stripped:
        raise ProtocolError("empty frame")

    try:
        text = stripped.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ProtocolError(f"invalid UTF-8: {e}") from e

    try:
        obj = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise ProtocolError(f"invalid JSON: {e}") from e

    if not isinstance(obj, dict):
        raise ProtocolError(
            f"expected JSON object, got {type(obj).__name__}"
        )

    # json.loads accepts bare NaN/Infinity in some edge cases depending on
    # the input. Walk the decoded object to reject non-finite floats.
    try:
        _reject_non_finite(obj, "")
    except RecursionError as error:
        raise ProtocolError("JSON nesting is too deep") from error

    return obj


def decode_request(raw: bytes) -> tuple[str, dict[str, Any]]:
    """
    Decode a client request. Returns (command_name, full_dict).

    Raises ProtocolError if the frame is invalid or missing 'cmd'.
    """
    obj = decode_line(raw)
    cmd = obj.get("cmd")
    if not isinstance(cmd, str) or not cmd:
        raise ProtocolError("request missing 'cmd' string field")
    return cmd, obj


def decode_reply(raw: bytes) -> dict[str, Any]:
    """
    Decode a daemon reply. Validates 'ok' field is a boolean.

    Raises ProtocolError if the frame is invalid or 'ok' is missing/wrong type.
    """
    obj = decode_line(raw)
    _validate_reply(obj)
    return obj


def _validate_reply(obj: dict[str, Any]) -> None:
    """Require the shared success flag and readable failure message."""
    if "ok" not in obj:
        raise ProtocolError("reply missing 'ok' field")
    if not isinstance(obj["ok"], bool):
        raise ProtocolError(f"reply 'ok' must be boolean, got {type(obj['ok']).__name__}")
    if not obj["ok"] and not isinstance(obj.get("error"), str):
        raise ProtocolError("failed reply requires an 'error' string")


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _reject_non_finite(obj: Any, path: str) -> None:
    """Walk a decoded JSON structure and reject any non-finite float values."""
    if isinstance(obj, float):
        import math
        if not math.isfinite(obj):
            raise ProtocolError(f"non-finite float at {path or 'root'}: {obj}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _reject_non_finite(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_non_finite(v, f"{path}[{i}]")
