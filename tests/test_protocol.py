"""Tests for nitrofan.protocol -- JSON-lines encode/decode and validation."""

import json
import math
import unittest

from nitrofan.protocol import (
    CMD_STATUS,
    CMD_SET_MODE,
    DEFAULT_SOCKET_PATH,
    KNOWN_COMMANDS,
    MAX_FRAME_BYTES,
    ProtocolError,
    decode_line,
    decode_reply,
    decode_request,
    encode_error,
    encode_reply,
    encode_request,
)


class TestEncodeRequest(unittest.TestCase):
    """Test request encoding."""

    def test_basic_request(self):
        raw = encode_request("status")
        obj = json.loads(raw)
        self.assertEqual(obj["cmd"], "status")

    def test_request_with_params(self):
        raw = encode_request("set_mode", mode="auto")
        obj = json.loads(raw)
        self.assertEqual(obj["cmd"], "set_mode")
        self.assertEqual(obj["mode"], "auto")

    def test_request_ends_with_newline(self):
        raw = encode_request("status")
        self.assertTrue(raw.endswith(b"\n"))

    def test_request_is_single_line(self):
        raw = encode_request("status")
        lines = raw.strip().split(b"\n")
        self.assertEqual(len(lines), 1)

    def test_request_is_utf8(self):
        raw = encode_request("status")
        # Should not raise
        raw.decode("utf-8")


class TestEncodeReply(unittest.TestCase):
    """Test reply encoding."""

    def test_ok_reply(self):
        raw = encode_reply(True, mode="auto", backend="mock")
        obj = json.loads(raw)
        self.assertTrue(obj["ok"])
        self.assertEqual(obj["mode"], "auto")

    def test_error_reply(self):
        raw = encode_error("bad command")
        obj = json.loads(raw)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["error"], "bad command")

    def test_nan_rejected(self):
        with self.assertRaises(ProtocolError):
            encode_reply(True, value=float("nan"))

    def test_infinity_rejected(self):
        with self.assertRaises(ProtocolError):
            encode_reply(True, value=float("inf"))

    def test_negative_infinity_rejected(self):
        with self.assertRaises(ProtocolError):
            encode_reply(True, value=float("-inf"))


class TestDecodeBasic(unittest.TestCase):
    """Test basic line decoding."""

    def test_valid_object(self):
        raw = b'{"cmd":"status"}\n'
        obj = decode_line(raw)
        self.assertEqual(obj["cmd"], "status")

    def test_no_trailing_newline(self):
        raw = b'{"cmd":"status"}'
        obj = decode_line(raw)
        self.assertEqual(obj["cmd"], "status")

    def test_empty_frame_rejected(self):
        with self.assertRaises(ProtocolError) as cm:
            decode_line(b"")
        self.assertIn("empty", str(cm.exception))

    def test_whitespace_only_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_line(b"   \n")

    def test_array_rejected(self):
        with self.assertRaises(ProtocolError) as cm:
            decode_line(b"[1,2,3]\n")
        self.assertIn("object", str(cm.exception))

    def test_bare_string_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_line(b'"hello"\n')

    def test_invalid_json_rejected(self):
        with self.assertRaises(ProtocolError) as cm:
            decode_line(b"{bad json}\n")
        self.assertIn("invalid JSON", str(cm.exception))

    def test_invalid_utf8_rejected(self):
        with self.assertRaises(ProtocolError) as cm:
            decode_line(b"\xff\xfe{}\n")
        self.assertIn("UTF-8", str(cm.exception))


class TestDecodeRequest(unittest.TestCase):
    """Test request decoding."""

    def test_valid_request(self):
        raw = b'{"cmd":"status"}\n'
        cmd, obj = decode_request(raw)
        self.assertEqual(cmd, "status")

    def test_missing_cmd_rejected(self):
        with self.assertRaises(ProtocolError) as cm:
            decode_request(b'{"mode":"auto"}\n')
        self.assertIn("cmd", str(cm.exception))

    def test_empty_cmd_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(b'{"cmd":""}\n')

    def test_cmd_not_string_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(b'{"cmd":42}\n')


class TestDecodeReply(unittest.TestCase):
    """Test reply decoding."""

    def test_failures_require_error_text_on_both_sides(self):
        for fields in ({}, {"error": None}, {"error": 42}):
            with self.subTest(fields=fields):
                with self.assertRaises(ProtocolError):
                    encode_reply(False, **fields)
                with self.assertRaises(ProtocolError):
                    decode_reply(json.dumps({"ok": False, **fields}).encode())
        with self.assertRaises(ProtocolError):
            encode_reply("yes")

    def test_ok_reply(self):
        raw = b'{"ok":true,"mode":"auto"}\n'
        obj = decode_reply(raw)
        self.assertTrue(obj["ok"])

    def test_error_reply(self):
        raw = b'{"ok":false,"error":"bad"}\n'
        obj = decode_reply(raw)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["error"], "bad")

    def test_missing_ok_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_reply(b'{"result":"done"}\n')

    def test_ok_not_boolean_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_reply(b'{"ok":1}\n')

    def test_ok_string_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_reply(b'{"ok":"true"}\n')


class TestFrameSize(unittest.TestCase):
    """Test frame size enforcement."""

    def test_oversized_frame_rejected_on_decode(self):
        big = b'{"x":"' + b"A" * MAX_FRAME_BYTES + b'"}\n'
        with self.assertRaises(ProtocolError) as cm:
            decode_line(big)
        self.assertIn("too large", str(cm.exception))

    def test_oversized_frame_rejected_on_encode(self):
        with self.assertRaises(ProtocolError):
            encode_reply(True, data="X" * MAX_FRAME_BYTES)

    def test_just_under_limit_accepted(self):
        # A frame near the limit but under should work
        payload = "X" * (MAX_FRAME_BYTES - 100)
        raw = encode_reply(True, data=payload)
        obj = decode_line(raw)
        self.assertTrue(obj["ok"])


class TestNonFiniteRejection(unittest.TestCase):
    """Python accepts non-finite JSON numbers by default; the protocol rejects them."""

    def test_non_finite_values_rejected_when_decoding(self):
        for value in (b"NaN", b"Infinity", b"-Infinity", b"1e999"):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                decode_line(b'{"temps":[' + value + b']}\n')

    def test_multiline_and_excessive_nesting_are_protocol_errors(self):
        for frame in (b'{\n"ok":true}\n', b'{"ok":true}\n\n',
                      b'{"data":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}\n'):
            with self.subTest(frame=frame[:30]), self.assertRaises(ProtocolError):
                decode_line(frame)
        self.assertEqual(decode_reply(b'{"ok":true}\r\n'), {"ok": True})

    def test_encode_nan_rejected(self):
        with self.assertRaises(ProtocolError):
            encode_request("test", temp=float("nan"))

    def test_encode_inf_in_list_rejected(self):
        with self.assertRaises(ProtocolError):
            encode_request("test", temps=[1.0, float("inf"), 3.0])

    def test_encode_negative_inf_in_nested_dict_rejected(self):
        with self.assertRaises(ProtocolError):
            encode_request("test", data={"x": float("-inf")})


class TestRoundTrip(unittest.TestCase):
    """Test encoding then decoding."""

    def test_request_round_trip(self):
        original = encode_request("set_mode", mode="curve", fan="cpu")
        cmd, obj = decode_request(original)
        self.assertEqual(cmd, "set_mode")
        self.assertEqual(obj["mode"], "curve")
        self.assertEqual(obj["fan"], "cpu")

    def test_reply_round_trip(self):
        original = encode_reply(True, mode="auto", temps={"cpu": 55, "gpu": 62})
        obj = decode_reply(original)
        self.assertTrue(obj["ok"])
        self.assertEqual(obj["temps"]["cpu"], 55)

    def test_error_round_trip(self):
        original = encode_error("unknown command")
        obj = decode_reply(original)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["error"], "unknown command")


class TestConstants(unittest.TestCase):
    """Verify protocol constants are defined."""

    def test_socket_path(self):
        self.assertEqual(DEFAULT_SOCKET_PATH, "/run/nitrofan/nitrofan.sock")

    def test_known_commands(self):
        self.assertIn("status", KNOWN_COMMANDS)
        self.assertIn("set_mode", KNOWN_COMMANDS)
        self.assertIn("set_curve", KNOWN_COMMANDS)
        self.assertIn("save", KNOWN_COMMANDS)
        self.assertIn("subscribe", KNOWN_COMMANDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
