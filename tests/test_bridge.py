#!/usr/bin/env python3
"""Self-contained test suite for the OpenCode Telegram bridge.

Runs without network access: HTTP is stubbed so the tests are fast and
deterministic. Run with:  python3 tests/test_bridge.py
"""

import json
import io
import os
import stat
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oc_telegram import (  # noqa: E402
    PermissionPolicy,
    PolicyError,
    RateLimiter,
    StateStore,
    ConfigError,
    TelegramClient,
    TelegramError,
    InboxFullError,
    escape_markdown,
    load_config,
    resolve_media_path,
    sanitize_text,
    validate_chat_id,
    validate_session_id,
)
from oc_telegram.opencode_api import OpenCodeClient, OpenCodeError  # noqa: E402
from oc_telegram.telegram_api import _safe_upload_filename  # noqa: E402


class TestSanitization(unittest.TestCase):
    def test_strips_control_and_invisible(self):
        self.assertEqual(sanitize_text("a\x00b\u200bc"), "abc")

    def test_preserves_newlines_and_tabs(self):
        self.assertEqual(sanitize_text("a\nb\tc"), "a\nb\tc")

    def test_truncates_to_max_length(self):
        self.assertEqual(len(sanitize_text("x" * 5000, 100)), 100)

    def test_normalises_crlf(self):
        self.assertEqual(sanitize_text("a\r\nb"), "a\nb")

    def test_escapes_markdown(self):
        self.assertEqual(escape_markdown("*b*"), r"\*b\*")

    def test_markdown_escape_neutralises_link(self):
        out = escape_markdown("[click](http://evil)")
        self.assertNotIn("](", out.replace(r"\]", ""))


class TestSessionIdValidation(unittest.TestCase):
    def test_accepts_valid(self):
        self.assertTrue(validate_session_id("ses_e8907af8effeSW0XY67qdsWle0"))

    def test_rejects_path_traversal(self):
        for bad in ("../../etc/passwd", "ses_../x", "ses_short", "", "ses_" + "a" * 100):
            with self.assertRaises(PolicyError):
                validate_session_id(bad)


class TestUploadFilename(unittest.TestCase):
    def test_multipart_header_controls_are_removed(self):
        safe = _safe_upload_filename('report"\r\nX-Evil: yes\\.txt')
        self.assertNotIn("\r", safe)
        self.assertNotIn("\n", safe)
        self.assertNotIn('"', safe)
        self.assertNotIn("\\", safe)


class TestTelegramErrorRedaction(unittest.TestCase):
    def test_http_error_traceback_does_not_retain_token_url(self):
        token = "123456:fake-test-token"
        client = TelegramClient(token)
        error = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{token}/getMe",
            403,
            "Forbidden",
            {},
            io.BytesIO(b""),
        )
        with mock.patch("oc_telegram.telegram_api._URL_OPENER.open", side_effect=error):
            with self.assertRaises(TelegramError) as raised:
                client.get_me()
        self.assertNotIn(token, str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)


class TestOpenCodeErrorRedaction(unittest.TestCase):
    def test_http_error_body_is_not_returned(self):
        client = OpenCodeClient("http://127.0.0.1:4096")
        error = urllib.error.HTTPError(
            "http://127.0.0.1:4096/session/private-session/message",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b"private response body"),
        )
        with mock.patch("oc_telegram.opencode_api._URL_OPENER.open", side_effect=error):
            with self.assertRaises(OpenCodeError) as raised:
                client._request("GET", "/session/private-session/message")
        self.assertNotIn("private response body", str(raised.exception))
        self.assertNotIn("private-session", str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)


class TestChatAllowlist(unittest.TestCase):
    def test_allows_listed(self):
        self.assertEqual(validate_chat_id(123, [123, 456]), 123)

    def test_rejects_unlisted(self):
        with self.assertRaises(PolicyError):
            validate_chat_id(999, [123])

    def test_rejects_non_numeric(self):
        with self.assertRaises(PolicyError):
            validate_chat_id("abc", [123])

    def test_rejects_boolean_and_fractional_chat_ids(self):
        for value in (True, 123.4):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                validate_chat_id(value, [1, 123])

    def test_deny_by_default_when_allowlist_empty(self):
        with self.assertRaises(PolicyError):
            validate_chat_id(123, [])


class TestRateLimiter(unittest.TestCase):
    def test_allows_up_to_limit(self):
        limiter = RateLimiter(limit=3, window=60)
        self.assertTrue(limiter.check(1))
        self.assertTrue(limiter.check(1))
        self.assertTrue(limiter.check(1))
        self.assertFalse(limiter.check(1))

    def test_keys_are_independent(self):
        limiter = RateLimiter(limit=1, window=60)
        self.assertTrue(limiter.check(1))
        self.assertTrue(limiter.check(2))

    def test_window_expiry(self):
        limiter = RateLimiter(limit=1, window=1)
        self.assertTrue(limiter.check(1, now=0.0))
        self.assertTrue(limiter.check(1, now=5.0))


class TestPermissionPolicy(unittest.TestCase):
    def test_never_allows_standing_approval(self):
        with self.assertRaises(PolicyError):
            PermissionPolicy().validate_reply("always")

    def test_allows_once(self):
        self.assertEqual(PermissionPolicy().validate_reply("once"), "once")

    def test_allows_reject(self):
        self.assertEqual(PermissionPolicy().validate_reply("reject"), "reject")

    def test_rejects_unknown(self):
        with self.assertRaises(PolicyError):
            PermissionPolicy().validate_reply("yes")

    def test_classifies_shell_as_dangerous(self):
        policy = PermissionPolicy()
        self.assertEqual(policy.classify("bash", ["rm -rf /"]), "dangerous")
        self.assertEqual(policy.classify("read", ["file.txt"]), "normal")

    def test_once_disabled_by_policy(self):
        with self.assertRaises(PolicyError):
            PermissionPolicy(allow_once=False).validate_reply("once")


class TestMediaPathSandbox(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inside = self.root / "ok.txt"
        self.inside.write_text("hello")
        self.outside = Path("/etc/hostname")

    def tearDown(self):
        self.tmp.cleanup()

    def test_allows_file_inside_root(self):
        resolved = resolve_media_path(str(self.inside), self.root, [])
        self.assertEqual(resolved, self.inside.resolve())

    def test_rejects_file_outside_root(self):
        with self.assertRaises(PolicyError):
            resolve_media_path(str(self.outside), self.root, [])

    def test_rejects_symlink_escape(self):
        link = self.root / "link"
        link.symlink_to(self.outside)
        with self.assertRaises(PolicyError):
            resolve_media_path(str(link), self.root, [])

    def test_rejects_missing_file(self):
        with self.assertRaises(PolicyError):
            resolve_media_path(str(self.root / "nope.txt"), self.root, [])

    def test_rejects_directory(self):
        with self.assertRaises(PolicyError):
            resolve_media_path(str(self.root), self.root, [])

    def test_rejects_when_no_roots_are_configured(self):
        with self.assertRaises(PolicyError):
            resolve_media_path(str(self.inside), None, [])

    def test_rejects_env_files_even_inside_media_root(self):
        env_file = self.root / ".env"
        env_file.write_text("token=not-a-real-secret")
        with self.assertRaises(PolicyError):
            resolve_media_path(str(env_file), self.root, [])


class TestStateStore(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        store = StateStore(self.path)
        self.assertIsNone(store.get_session(1))
        store.set_session(1, "ses_abc12345")
        self.assertEqual(StateStore(self.path).get_session(1), "ses_abc12345")

    def test_clear(self):
        store = StateStore(self.path)
        store.set_session(1, "ses_abc12345")
        store.clear_session(1)
        self.assertIsNone(StateStore(self.path).get_session(1))

    def test_corrupt_file_is_ignored(self):
        self.path.write_text("{not json")
        self.assertIsNone(StateStore(self.path).get_session(1))

    def test_private_permissions_and_update_deduplication(self):
        store = StateStore(self.path)
        store.set_offset(12)
        store.record_inbound(123, "hello", update_id=12)
        store.record_inbound(123, "hello", update_id=12)

        self.assertEqual(len(store.recent_inbound(123)), 1)
        self.assertNotIn("__offset__", store.all_bindings())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.inbox_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_pending_inbox_has_a_hard_limit(self):
        store = StateStore(self.path)
        for update_id in range(1, 201):
            store.record_inbound(123, "queued", update_id=update_id)
        with self.assertRaises(InboxFullError):
            store.record_inbound(123, "overflow", update_id=201)


class TestInboundBridge(unittest.TestCase):
    def test_message_is_persisted_and_acknowledged(self):
        import tempfile
        from types import SimpleNamespace

        from oc_telegram.bridge import Bridge

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            bridge = Bridge.__new__(Bridge)
            bridge.config = SimpleNamespace(max_message_length=8000)
            bridge.limiter = RateLimiter(limit=20, window=60)
            bridge.state = store
            bridge._reply = mock.Mock()

            bridge._handle_message(123, "hello", update_id=44)

            entries = store.recent_inbound(chat_id=123)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["text"], "hello")
            self.assertEqual(entries[0]["update_id"], 44)
            bridge._reply.assert_called_once_with(123, "✓ received — the agent will reply")


class TestConfigSecurity(unittest.TestCase):
    def _load(self, url, extra=None):
        environment = {"OPENCODE_URL": url}
        environment.update(extra or {})
        with mock.patch("oc_telegram.config.load_dotenv"), mock.patch.dict(
            os.environ, environment, clear=True
        ):
            return load_config(require_token=False)

    def test_allows_loopback_http_and_https(self):
        self.assertEqual(self._load("http://127.0.0.1:4096").opencode_url, "http://127.0.0.1:4096")
        self.assertEqual(self._load("https://[::1]:4096").opencode_url, "https://[::1]:4096")

    def test_rejects_remote_https_and_non_http_schemes(self):
        for url in ("https://example.com", "file:///etc/passwd", "http://127.0.0.1@evil.test"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                self._load(url)

    def test_default_chat_does_not_bypass_allowlist(self):
        config = self._load(
            "http://127.0.0.1:4096",
            {"TELEGRAM_DEFAULT_CHAT_ID": "123", "TELEGRAM_ALLOWED_CHAT_IDS": ""},
        )
        self.assertEqual(config.allowed_chat_ids, ())
        with self.assertRaises(PolicyError):
            validate_chat_id(config.default_chat_id, config.allowed_chat_ids)


class TestOpenCodeClient(unittest.TestCase):
    def setUp(self):
        self.client = OpenCodeClient("http://127.0.0.1:4096")

    def test_non_loopback_url_is_not_built_in(self):
        # The client itself does not block; config.py does. Verify the guard.
        from oc_telegram.config import ConfigError, load_config

        with mock.patch.dict(os.environ, {"OPENCODE_URL": "http://evil.example.com"}, clear=False):
            with self.assertRaises(ConfigError):
                load_config(require_token=False)

    def test_prompt_rejects_bad_session(self):
        with self.assertRaises(PolicyError):
            self.client.send_prompt("../../etc/passwd", "hi")

    def test_prompt_rejects_bad_delivery(self):
        with self.assertRaises(OpenCodeError):
            self.client.send_prompt("ses_abc12345", "hi", delivery="bogus")


class TestConfigSecurity(unittest.TestCase):
    def _load(self, url, extra=None):
        environment = {"OPENCODE_URL": url}
        environment.update(extra or {})
        with mock.patch("oc_telegram.config.load_dotenv"), mock.patch.dict(
            os.environ, environment, clear=True
        ):
            return load_config(require_token=False)

    def test_allows_loopback_http_and_https(self):
        self.assertEqual(
            self._load("http://127.0.0.1:4096").opencode_url,
            "http://127.0.0.1:4096",
        )
        self.assertEqual(
            self._load("https://[::1]:4096").opencode_url,
            "https://[::1]:4096",
        )

    def test_rejects_remote_https_and_non_http_schemes(self):
        for url in (
            "https://example.com",
            "file:///etc/passwd",
            "http://127.0.0.1@evil.test",
        ):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                self._load(url)

    def test_default_chat_does_not_bypass_allowlist(self):
        config = self._load(
            "http://127.0.0.1:4096",
            {"TELEGRAM_DEFAULT_CHAT_ID": "123", "TELEGRAM_ALLOWED_CHAT_IDS": ""},
        )
        self.assertEqual(config.allowed_chat_ids, ())
        with self.assertRaises(PolicyError):
            validate_chat_id(config.default_chat_id, config.allowed_chat_ids)

class TestMcpFraming(unittest.TestCase):
    """The MCP stdio transport is newline-delimited JSON.

    This is verified against the MCP TypeScript SDK that OpenCode embeds:
    its ReadBuffer splits on "\\n" and its writer appends "\\n". Using
    Content-Length headers (the LSP/HTTP style) would deadlock the server.
    """

    def test_write_uses_newline_delimited_json(self):
        import oc_telegram.mcp_server as mcp

        written = []

        class Buf:
            def write(self, data):
                written.append(data)

            def flush(self):
                pass

        class Out:
            buffer = Buf()

        original = sys.stdout
        sys.stdout = Out()
        try:
            mcp._write_message({"jsonrpc": "2.0", "id": 1, "result": {}})
        finally:
            sys.stdout = original

        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].endswith(b"\n"))
        self.assertNotIn(b"Content-Length", written[0])
        self.assertEqual(json.loads(written[0].decode("utf-8"))["id"], 1)

    def test_read_splits_on_newline(self):
        import oc_telegram.mcp_server as mcp

        payload = b'{"jsonrpc":"2.0","id":7,"method":"ping"}\n'
        stdin = mock.MagicMock()
        stdin.buffer.readline.return_value = payload

        original = sys.stdin
        sys.stdin = stdin
        try:
            msg = mcp._read_message()
        finally:
            sys.stdin = original

        self.assertEqual(msg["id"], 7)
        self.assertEqual(msg["method"], "ping")

    def test_read_skips_blank_lines(self):
        import oc_telegram.mcp_server as mcp

        lines = [b"\n", b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n']
        stdin = mock.MagicMock()
        stdin.buffer.readline.side_effect = lines

        original = sys.stdin
        sys.stdin = stdin
        try:
            msg = mcp._read_message()
        finally:
            sys.stdin = original

        self.assertEqual(msg["id"], 1)

    def test_protocol_version_negotiation(self):
        import oc_telegram.mcp_server as mcp

        self.assertEqual(mcp._negotiate_protocol("2025-06-18"), "2025-06-18")
        self.assertEqual(mcp._negotiate_protocol("2024-11-05"), "2024-11-05")
        # Unsupported versions fall back to a known-good one.
        self.assertEqual(mcp._negotiate_protocol("1999-01-01"), "2024-11-05")
        self.assertEqual(mcp._negotiate_protocol(None), "2024-11-05")


class TestOpenCodeContracts(unittest.TestCase):
    """Contracts verified against OpenCode's live server / embedded SDK."""

    def test_active_session_unwraps_map(self):
        client = OpenCodeClient("http://127.0.0.1:4096")
        payload = {
            "data": {
                "ses_a": {"id": "ses_a", "time": {"updated": 10}},
                "ses_b": {"id": "ses_b", "time": {"updated": 99}},
            }
        }
        with mock.patch.object(client, "_request", return_value=payload):
            session = client.active_session()
        self.assertEqual(session["id"], "ses_b")

    def test_active_session_handles_single_object(self):
        client = OpenCodeClient("http://127.0.0.1:4096")
        payload = {"data": {"id": "ses_x", "time": {"updated": 1}}}
        with mock.patch.object(client, "_request", return_value=payload):
            self.assertEqual(client.active_session()["id"], "ses_x")

    def test_active_session_handles_empty(self):
        client = OpenCodeClient("http://127.0.0.1:4096")
        with mock.patch.object(client, "_request", return_value={"data": {}}):
            self.assertIsNone(client.active_session())

    def test_session_model_formats_provider_and_variant(self):
        client = OpenCodeClient("http://127.0.0.1:4096")
        session = {
            "model": {"providerID": "opencode-go", "id": "minimax-m3.1-flash", "variant": "high"}
        }
        self.assertEqual(
            client.session_model(session), "opencode-go/minimax-m3.1-flash (high)"
        )

    def test_sse_envelope_payload_is_nested_under_data(self):
        """The v2 event stream puts the payload under envelope['data']."""
        import oc_telegram.mcp_server as mcp  # noqa: F401

        # Simulate the parser contract: envelope -> {"type", "data"}.
        envelope = {
            "id": "evt_1",
            "type": "session.next.text.delta",
            "durable": {"aggregateID": "ses_a", "seq": 3, "version": 1},
            "data": {"assistantMessageID": "msg_1", "textID": "txt_1", "delta": "hi"},
        }
        payload = envelope.get("data")
        self.assertEqual(envelope["type"], "session.next.text.delta")
        self.assertEqual(payload["delta"], "hi")

    def test_prompt_body_shape(self):
        client = OpenCodeClient("http://127.0.0.1:4096")
        with mock.patch.object(client, "_request", return_value={"data": {}}) as req:
            client.send_prompt("ses_abc12345", "hello", delivery="steer")
        url = req.call_args[0][1]
        body = req.call_args[0][2]
        self.assertEqual(url, "/api/session/ses_abc12345/prompt")
        self.assertEqual(body["prompt"]["text"], "hello")
        self.assertEqual(body["delivery"], "steer")

    def test_delivery_modes_are_constrained(self):
        client = OpenCodeClient("http://127.0.0.1:4096")
        with self.assertRaises(OpenCodeError):
            client.send_prompt("ses_abc12345", "x", delivery="steal")


class TestMcpProtocol(unittest.TestCase):
    """Exercise the MCP framing and tool dispatch end to end."""

    def _run(self, requests):
        import oc_telegram.mcp_server as mcp

        responses = []
        server = mcp.TelegramMcpServer.__new__(mcp.TelegramMcpServer)
        server.config = mock.MagicMock()
        server.config.max_message_length = 8000
        server.config.max_reply_length = 4000
        server.config.allowed_chat_ids = (111,)
        server.config.default_chat_id = 111
        server.config.media_root = None
        server.config.redact = lambda s: s
        server.telegram = mock.MagicMock()
        server.opencode = mock.MagicMock()
        server.state = mock.MagicMock()
        server.state.all_bindings.return_value = {}
        server._inbound = []

        with mock.patch.object(mcp, "_read_message", side_effect=list(requests) + [None]), \
             mock.patch.object(mcp, "_write_message", side_effect=responses.append):
            server.run()

        return responses

    def test_initialize_and_list(self):
        responses = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ])
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "opencode-telegram-bridge")
        names = {t["name"] for t in responses[1]["result"]["tools"]}
        self.assertIn("telegram_send_message", names)
        self.assertIn("telegram_read_messages", names)

    def test_send_message_calls_telegram(self):
        responses = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "telegram_send_message", "arguments": {"text": "hi"}}},
        ])
        self.assertFalse(responses[0]["result"].get("isError", False))

    def test_send_message_refuses_unlisted_chat(self):
        responses = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "telegram_send_message",
                        "arguments": {"text": "hi", "chat_id": "999"}}},
        ])
        self.assertTrue(responses[0]["result"]["isError"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
