"""Inbound bridge: Telegram -> the live OpenCode session.

How it works
------------
1. Long-polls the Telegram Bot API for new messages.
2. Drops anything from a chat that is not on the allowlist.
3. Sanitises the text and posts it to the OpenCode session with
   ``POST /api/session/{id}/prompt`` - the same call the TUI makes when you
   type. The session keeps its own model, agent, tools and history.
4. Subscribes to the session event stream and collects the assistant text
   produced for that turn.
5. If the agent raises a permission request, the bridge asks the user in
   Telegram instead of approving anything on its own.
6. Sends the reply back to Telegram.

Security posture
----------------
* Deny-by-default: with no allowlist configured, nothing is accepted.
* A Telegram message can only *steer* the agent. It can never widen the
  agent's own permissions - those always require an explicit human answer.
* The bot token is never logged and never included in tool output.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oc_telegram import (
    Config,
    ConfigError,
    InboxFullError,
    OpenCodeClient,
    OpenCodeError,
    PolicyError,
    RateLimiter,
    StateStore,
    TelegramClient,
    TelegramError,
    escape_markdown,
    load_config,
    sanitize_text,
    validate_chat_id,
    validate_session_id,
)

LOG = logging.getLogger("oc-telegram-bridge")

# Telegram commands understood by the bridge.
COMMANDS = ("/start", "/help", "/bind", "/unbind", "/sessions", "/status", "/approve", "/reject", "/stop")


class Bridge:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.telegram = TelegramClient(config.bot_token)
        self.opencode = OpenCodeClient(
            config.opencode_url,
            config.opencode_username,
            config.opencode_password,
            config.opencode_timeout,
        )
        self.state = StateStore(config.state_file)
        self.limiter = RateLimiter(config.rate_limit_count, config.rate_limit_window)
        self._offset: Optional[int] = self.state.get_offset()
        self._stop = threading.Event()

    # -- session resolution -------------------------------------------------

    def _target_session(self, chat_id: int) -> Optional[Dict[str, Any]]:
        bound = self.state.get_session(chat_id)
        try:
            return self.opencode.resolve_session(bound)
        except OpenCodeError as exc:
            LOG.warning("session lookup failed: %s", exc)
            if bound:
                # The bound session vanished; fall back and re-bind.
                self.state.clear_session(chat_id)
                return self.opencode.resolve_session(None)
            return None

    def _server_reachable(self) -> bool:
        """Distinguish "no server running" from "server up, no sessions yet"."""
        try:
            self.opencode.health()
            return True
        except OpenCodeError:
            return False

    # -- outbound helpers ---------------------------------------------------

    def _reply(self, chat_id: int, text: str, markdown: bool = False) -> None:
        if not text:
            return
        body = escape_markdown(text) if markdown else text
        try:
            self.telegram.send_message(chat_id, body, markdown=markdown)
        except TelegramError as exc:
            LOG.error("failed to send to Telegram: %s", self.config.redact(str(exc)))

    # -- commands -----------------------------------------------------------

    def _handle_command(self, chat_id: int, text: str) -> bool:
        parts = text.strip().split()
        command = parts[0].lower().split("@")[0]

        if command in ("/start", "/help"):
            self._reply(
                chat_id,
                "OpenCode Telegram bridge.\n\n"
                "Send any message and it is delivered to the active OpenCode "
                "session, using that session's current model.\n\n"
                "Commands:\n"
                "/status - show the bound session and model\n"
                "/sessions - list recent OpenCode sessions\n"
                "/bind <session_id> - pin this chat to a session\n"
                "/unbind - go back to the active session\n"
                "/approve - approve a pending permission once\n"
                "/reject - reject a pending permission\n"
                "/stop - interrupt the running turn",
            )
            return True

        if command == "/status":
            session = self._target_session(chat_id)
            if not session:
                self._reply(chat_id, "No OpenCode session is available.")
                return True
            self._reply(
                chat_id,
                f"session: {session.get('id')}\n"
                f"model: {self.opencode.session_model(session)}\n"
                f"title: {str(session.get('title', ''))[:80]}",
            )
            return True

        if command == "/sessions":
            try:
                sessions = self.opencode.list_sessions(limit=8)
            except OpenCodeError as exc:
                self._reply(chat_id, f"Could not list sessions: {exc}")
                return True
            lines = ["Recent OpenCode sessions:"]
            for session in sessions:
                lines.append(
                    f"  {session.get('id')}  {self.opencode.session_model(session)}  "
                    f"{str(session.get('title', ''))[:50]}"
                )
            self._reply(chat_id, "\n".join(lines))
            return True

        if command == "/bind":
            if len(parts) < 2:
                self._reply(chat_id, "Usage: /bind <session_id>")
                return True
            try:
                session_id = validate_session_id(parts[1])
                session = self.opencode.get_session(session_id)
            except (PolicyError, OpenCodeError) as exc:
                self._reply(chat_id, f"Could not bind: {exc}")
                return True
            if not session:
                self._reply(chat_id, f"Session {parts[1]} was not found.")
                return True
            self.state.set_session(chat_id, session_id)
            self.state.set_title(chat_id, str(session.get("title", ""))[:80])
            self._reply(
                chat_id,
                f"Bound this chat to {session_id}\n"
                f"model: {self.opencode.session_model(session)}",
            )
            return True

        if command == "/unbind":
            self.state.clear_session(chat_id)
            self._reply(chat_id, "Unbound. This chat now targets the active session.")
            return True

        if command in ("/approve", "/reject"):
            self._reply(
                chat_id,
                "Telegram permission approvals are disabled. Review and answer "
                "permission requests in the OpenCode UI.",
            )
            return True

        if command == "/stop":
            session = self._target_session(chat_id)
            if session:
                try:
                    self.opencode._request(  # noqa: SLF001 - documented endpoint
                        "POST", f"/api/session/{session['id']}/interrupt", {}
                    )
                    self._reply(chat_id, "Interrupted the running turn.")
                except OpenCodeError as exc:
                    self._reply(chat_id, f"Could not interrupt: {exc}")
            return True

        return False

    # -- turn handling ------------------------------------------------------

    def _handle_message(
        self, chat_id: int, text: str, update_id: Optional[int] = None
    ) -> None:
        if not self.limiter.check(chat_id):
            wait = self.limiter.retry_after(chat_id)
            self._reply(
                chat_id,
                f"Rate limit reached. Try again in {wait}s.",
            )
            return

        if text.strip().lower() in COMMANDS or text.strip().startswith("/"):
            if self._handle_command(chat_id, text):
                return

        clean = sanitize_text(text, self.config.max_message_length)
        if not clean:
            return

        # Delivery is handled in-process by the opencode plugin
        # (telegram-inbox), which reads the shared inbox and injects the
        # message into the session the user is viewing, then relays the reply.
        # The bridge only records the message and acknowledges receipt.
        self.state.record_inbound(chat_id, clean, update_id=update_id)
        self._reply(chat_id, "✓ received — the agent will reply")

    @staticmethod
    def _split(text: str, size: int) -> List[str]:
        chunks: List[str] = []
        while text:
            if len(text) <= size:
                chunks.append(text)
                break
            cut = text.rfind("\n", 0, size)
            if cut < size // 2:
                cut = size
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        return chunks

    # -- main loop ----------------------------------------------------------

    def run(self) -> None:
        try:
            me = self.telegram.get_me()
            LOG.info("connected to Telegram bot @%s", me.get("username", "?"))
        except TelegramError as exc:
            raise SystemExit(f"Cannot reach Telegram: {exc}")

        try:
            self.telegram.set_webhook()
        except TelegramError:
            LOG.warning("could not clear webhook; long polling may fail")

        LOG.info(
            "polling for messages (allowlist_count=%d, opencode=%s)",
            len(self.config.allowed_chat_ids),
            self.config.opencode_url,
        )

        while not self._stop.is_set():
            try:
                updates = self.telegram.get_updates(
                    self._offset, timeout=self.config.poll_timeout
                )
            except TelegramError as exc:
                if exc.error_code == 409:
                    # Another consumer is polling this bot token. Backing off
                    # is correct: two pollers fight over the same updates.
                    LOG.warning(
                        "another getUpdates consumer holds this bot token; "
                        "backing off"
                    )
                    self._stop.wait(30)
                    continue
                LOG.warning("poll failed: %s", self.config.redact(str(exc)))
                self._stop.wait(3)
                continue

            for update in updates:
                update_id = update.get("update_id")
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                text = message.get("text")

                if chat_id is None or not text:
                    if update_id is not None:
                        if not self._commit_offset(update_id):
                            break
                    continue

                try:
                    validate_chat_id(chat_id, self.config.allowed_chat_ids)
                except PolicyError:
                    LOG.warning("rejected message from a non-allowlisted chat")
                    if update_id is not None:
                        if not self._commit_offset(update_id):
                            break
                    continue

                LOG.info("received message from an allowlisted chat")
                try:
                    self._handle_message(int(chat_id), text, update_id=update_id)
                except InboxFullError:
                    LOG.warning("Telegram inbox full; new update was not queued")
                    self._reply(
                        int(chat_id),
                        "The local message queue is full. Please wait for OpenCode to finish, then resend.",
                    )
                    if update_id is not None and not self._commit_offset(update_id):
                        break
                except Exception:  # noqa: BLE001
                    LOG.error("failed to handle an allowlisted Telegram update")
                    self._reply(int(chat_id), "The bridge failed to handle that message.")
                    break
                if update_id is not None:
                    if not self._commit_offset(update_id):
                        break

    def _commit_offset(self, update_id: int) -> bool:
        next_offset = int(update_id) + 1
        try:
            self.state.set_offset(next_offset)
        except OSError:
            LOG.error("could not persist the Telegram update cursor")
            return False
        self._offset = next_offset
        return True

    def stop(self, *_args: Any) -> None:
        LOG.info("shutting down")
        self._stop.set()


def main() -> int:
    logging.basicConfig(
        level=os.getenv("OC_TELEGRAM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        config = load_config(require_token=True)
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        return 2

    if not config.allowed_chat_ids:
        LOG.warning(
            "No TELEGRAM_ALLOWED_CHAT_IDS configured - all inbound messages are blocked."
        )

    bridge = Bridge(config)
    signal.signal(signal.SIGINT, bridge.stop)
    signal.signal(signal.SIGTERM, bridge.stop)

    try:
        bridge.run()
    except SystemExit as exc:
        LOG.error("%s", exc)
        return 1
    except Exception:  # noqa: BLE001
        LOG.exception("fatal error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
