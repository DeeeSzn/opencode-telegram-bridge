"""MCP server exposing Telegram to OpenCode.

Transport: stdio, JSON-RPC 2.0 with **newline-delimited** framing.

Framing is not negotiable here. The MCP TypeScript SDK's StdioClientTransport
deliberately splits on "\\n" and writes `JSON.stringify(message) + "\\n"`.
The LSP-style `Content-Length:` headers belong to the HTTP transport, not
stdio; a server that uses them will appear to hang with no output.

Tools
-----
telegram_send_message  Send a message to a Telegram chat.
telegram_read_messages Read recent messages received by the bot.
telegram_send_document Attach a file to a Telegram chat.
telegram_session_info  Show the bound OpenCode session and its model.

Every tool is subject to the same policy: only allowlisted chats may be
targeted, media must live under a permitted root, and the bot token is never
echoed back.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oc_telegram import (
    Config,
    ConfigError,
    OpenCodeClient,
    OpenCodeError,
    PolicyError,
    StateStore,
    TelegramClient,
    TelegramError,
    escape_markdown,
    load_config,
    sanitize_text,
    validate_chat_id,
    resolve_media_path,
)

LOG = logging.getLogger("oc-telegram-mcp")

# Protocol versions the MCP SDK accepts, newest first.
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
    "2024-10-07",
)
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

SERVER_NAME = "opencode-telegram-bridge"
SERVER_VERSION = "1.0.0"


# --- MCP framing ------------------------------------------------------------


def _read_message() -> Optional[Dict[str, Any]]:
    """Read one newline-delimited JSON-RPC message from stdin.

    Mirrors the MCP SDK's ReadBuffer: split on "\\n", drop a trailing "\\r",
    then JSON.parse. Blank lines are skipped.
    """
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        stripped = line.rstrip(b"\r\n")
        if not stripped.strip():
            continue
        try:
            payload = json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            LOG.warning("discarding malformed MCP frame")
            continue
        return payload if isinstance(payload, dict) else None


def _write_message(payload: Dict[str, Any]) -> None:
    """Write one newline-delimited JSON-RPC message to stdout."""
    sys.stdout.buffer.write(json.dumps(payload).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _negotiate_protocol(requested: Optional[str]) -> str:
    """Echo the client's requested protocol version when we support it."""
    if requested and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return DEFAULT_PROTOCOL_VERSION


def _text_response(text: str, is_error: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


# --- tool definitions -------------------------------------------------------

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "telegram_send_message",
        "description": (
            "Send a message to a Telegram chat. Use this to proactively notify "
            "the user during an OpenCode session. If chat_id is omitted the "
            "configured default chat is used."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Message body to send.",
                },
                "chat_id": {
                    "type": "string",
                    "description": "Target Telegram chat id (defaults to the configured chat).",
                },
                "markdown": {
                    "type": "boolean",
                    "description": "Render the text as Telegram Markdown (default false).",
                    "default": False,
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "telegram_read_messages",
        "description": (
            "Read recent messages the bot has received. Use this to check for "
            "instructions the user sent from Telegram."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to return (default 20).",
                    "default": 20,
                },
                "chat_id": {
                    "type": "string",
                    "description": "Chat id to read from (defaults to the configured chat).",
                },
            },
        },
    },
    {
        "name": "telegram_send_document",
        "description": (
            "Send a file from disk to a Telegram chat. The path must resolve "
            "inside the configured media root."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path of the file to send.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption.",
                    "default": "",
                },
                "chat_id": {
                    "type": "string",
                    "description": "Target Telegram chat id (defaults to the configured chat).",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "telegram_session_info",
        "description": (
            "Show which OpenCode session this bridge is bound to, including the "
            "model and provider that session is currently using."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# --- server -----------------------------------------------------------------


class TelegramMcpServer:
    def __init__(self) -> None:
        self.config: Config = load_config(require_token=True)
        self.telegram = TelegramClient(self.config.bot_token)
        self.opencode = OpenCodeClient(
            self.config.opencode_url,
            self.config.opencode_username,
            self.config.opencode_password,
            self.config.opencode_timeout,
        )
        self.state = StateStore(self.config.state_file)
        self._inbound: List[Dict[str, Any]] = []

    # -- helpers ------------------------------------------------------------

    def _resolve_chat(self, raw: Optional[str]) -> int:
        candidate = raw if raw not in (None, "") else self.config.default_chat_id
        if candidate is None:
            raise PolicyError(
                "No chat id provided and TELEGRAM_DEFAULT_CHAT_ID is not configured."
            )
        return validate_chat_id(candidate, self.config.allowed_chat_ids)

    def _record_inbound(self, chat_id: int, text: str) -> None:
        self._inbound.append({"chat_id": chat_id, "text": text, "at": _now()})
        del self._inbound[:-200]

    # -- tool handlers ------------------------------------------------------

    def _tool_send_message(self, args: Dict[str, Any]) -> Dict[str, Any]:
        text = sanitize_text(
            str(args.get("text", "")), self.config.max_message_length
        )
        if not text:
            return _text_response("text is required and must not be empty.", True)

        chat_id = self._resolve_chat(args.get("chat_id"))
        use_markdown = bool(args.get("markdown", False))
        body = escape_markdown(text) if use_markdown else text

        self.telegram.send_message(chat_id, body, markdown=use_markdown)
        return _text_response(f"Message sent to Telegram chat {chat_id}.")

    def _tool_read_messages(self, args: Dict[str, Any]) -> Dict[str, Any]:
        limit = max(1, min(100, int(args.get("limit", 20) or 20)))
        chat_id = self._resolve_chat(args.get("chat_id"))

        # Read from the shared inbox written by the bridge daemon rather than
        # calling getUpdates here: Telegram allows only one long-poller per
        # bot token, so polling from the MCP too would fight the bridge.
        entries = self.state.recent_inbound(chat_id=chat_id, limit=limit)
        if not entries:
            return _text_response(
                "No recent inbound messages for that chat "
                "(the bridge records messages as it delivers them)."
            )

        lines: List[str] = []
        for entry in entries:
            sender = entry.get("sender") or "user"
            body = entry.get("text") or "[non-text message]"
            at = entry.get("at")
            if at:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(at)))
                lines.append(f"[{stamp}] {sender}: {body}")
            else:
                lines.append(f"{sender}: {body}")
        return _text_response("\n".join(lines))

    def _tool_send_document(self, args: Dict[str, Any]) -> Dict[str, Any]:
        chat_id = self._resolve_chat(args.get("chat_id"))
        caption = sanitize_text(str(args.get("caption", "")), 1024)

        roots = [self.config.media_root] if self.config.media_root is not None else []
        resolved = resolve_media_path(
            str(args.get("file_path", "")), None, roots
        )
        self.telegram.send_document(chat_id, resolved, caption)
        return _text_response(f"Document sent to Telegram chat {chat_id}.")

    def _tool_session_info(self, args: Dict[str, Any]) -> Dict[str, Any]:
        lines: List[str] = []
        try:
            health = self.opencode.health()
            lines.append(f"OpenCode server: {health.get('version', 'unknown')}")
        except OpenCodeError as exc:
            lines.append(f"OpenCode server unreachable: {exc}")

        bindings = self.state.all_bindings()
        if not bindings:
            lines.append("No session is bound yet (use /bind in Telegram).")
        for chat_id, entry in bindings.items():
            session_id = entry.get("session_id", "")
            model = "unknown"
            title = entry.get("title", "")
            if session_id:
                try:
                    session = self.opencode.get_session(session_id)
                    if session:
                        model = self.opencode.session_model(session)
                        title = title or str(session.get("title", ""))[:60]
                except OpenCodeError as exc:
                    model = f"unreachable ({exc})"
            lines.append(
                f"chat {chat_id} -> session {session_id or '(none)'} | model {model}"
                + (f" | {title}" if title else "")
            )
        return _text_response("\n".join(lines))

    # -- dispatch ------------------------------------------------------------

    def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        handlers = {
            "telegram_send_message": self._tool_send_message,
            "telegram_read_messages": self._tool_read_messages,
            "telegram_send_document": self._tool_send_document,
            "telegram_session_info": self._tool_session_info,
        }
        handler = handlers.get(name)
        if handler is None:
            return _text_response(f"Unknown tool: {name}", True)
        try:
            return handler(args)
        except PolicyError as exc:
            return _text_response(f"Refused by policy: {exc}", True)
        except TelegramError as exc:
            return _text_response(
                f"Telegram API error: {self.config.redact(str(exc))}", True
            )
        except OpenCodeError as exc:
            return _text_response(f"OpenCode error: {exc}", True)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("tool %s failed", name)
            return _text_response(
                f"Unexpected error: {self.config.redact(str(exc))}", True
            )

    def run(self) -> None:
        LOG.info("MCP server ready (stdio)")
        while True:
            message = _read_message()
            if message is None:
                break
            method = message.get("method")
            msg_id = message.get("id")
            params = message.get("params") or {}

            if method == "initialize":
                result: Any = {
                    "protocolVersion": _negotiate_protocol(
                        params.get("protocolVersion")
                    ),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.call_tool(
                    str(params.get("name", "")), params.get("arguments") or {}
                )
            elif method in ("notifications/initialized", "notifications/cancelled"):
                continue
            elif method == "ping":
                result = {}
            else:
                if msg_id is not None:
                    _write_message(
                        {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "error": {
                                "code": -32601,
                                "message": f"Method not found: {method}",
                            },
                        }
                    )
                continue

            if msg_id is not None:
                _write_message({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _now() -> float:
    import time

    return time.time()


def main() -> int:
    logging.basicConfig(
        level=os.getenv("OC_TELEGRAM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        server = TelegramMcpServer()
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        LOG.error("failed to start: %s", exc)
        return 1
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
