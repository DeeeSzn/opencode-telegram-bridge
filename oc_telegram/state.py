"""Persistent state: Telegram chat -> OpenCode session bindings."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

# The shared inbound inbox is trimmed to this many recent messages. It is the
# single source of truth for "what did the bridge receive", so the MCP read
# tool never has to call getUpdates itself (which would fight the bridge for
# the bot token - Telegram allows only one long-poller per token).
INBOX_CAP = 200


class InboxFullError(OSError):
    """Raised when too many Telegram updates are waiting for OpenCode."""


class StateStore:
    """A tiny JSON file mapping Telegram chat ids to OpenCode session ids.

    Writes are atomic (temp file + rename) so a crash cannot leave a
    half-written state file that would strand the bridge.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.inbox_path = path.with_name("inbox.jsonl")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.path.parent, 0o700)
        self._data: Dict[str, Dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self._data = {k: v for k, v in data.items() if isinstance(v, dict)}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get_session(self, chat_id: int) -> Optional[str]:
        entry = self._data.get(str(chat_id))
        if not entry:
            return None
        return entry.get("session_id")

    def get_offset(self) -> Optional[int]:
        """The last Telegram update_id that has been processed."""
        value = self._data.get("__offset__")
        if isinstance(value, dict):
            value = value.get("update_id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def set_offset(self, offset: int) -> None:
        """Persist the processed update_id so a restart does not replay."""
        self._data["__offset__"] = {"update_id": int(offset)}
        self._save()

    def set_session(self, chat_id: int, session_id: str) -> None:
        self._data[str(chat_id)] = {
            "session_id": session_id,
            "title": self._data.get(str(chat_id), {}).get("title", ""),
        }
        self._save()

    def set_title(self, chat_id: int, title: str) -> None:
        entry = self._data.setdefault(str(chat_id), {})
        entry["title"] = title
        self._save()

    def clear_session(self, chat_id: int) -> None:
        if self._data.pop(str(chat_id), None) is not None:
            self._save()

    def all_bindings(self) -> Dict[str, Dict[str, str]]:
        return {key: value for key, value in self._data.items() if not key.startswith("__")}

    # -- shared inbound inbox ------------------------------------------------

    def record_inbound(
        self,
        chat_id: int,
        text: str,
        sender: str = "",
        update_id: Optional[int] = None,
    ) -> None:
        """Append a received Telegram message to the shared inbox file."""
        if update_id is not None and any(
            entry.get("update_id") == int(update_id)
            for entry in self.recent_inbound(limit=INBOX_CAP * 2)
        ):
            return

        if update_id is not None and self._pending_inbox_count() >= INBOX_CAP:
            raise InboxFullError("Telegram inbox is full; OpenCode has not consumed queued updates.")

        entry = {
            "chat_id": int(chat_id),
            "sender": sender,
            "text": text,
            "at": time.time(),
        }
        if update_id is not None:
            entry["update_id"] = int(update_id)
        fd = os.open(self.inbox_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        self._trim_inbox()

    def recent_inbound(
        self, chat_id: Optional[int] = None, limit: int = 20
    ) -> List[Dict[str, object]]:
        """Return the most recent recorded inbound messages, newest last."""
        if not self.inbox_path.is_file():
            return []
        entries: List[Dict[str, object]] = []
        try:
            raw = self.inbox_path.read_text(encoding="utf-8")
        except OSError:
            return []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if chat_id is not None and obj.get("chat_id") != int(chat_id):
                continue
            entries.append(obj)
        return entries[-limit:]

    def _trim_inbox(self) -> None:
        """Trim only acknowledged records, preserving unprocessed updates."""
        try:
            lines = self.inbox_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) <= INBOX_CAP * 2:
            return

        cursor = self._plugin_cursor()
        completed: List[str] = []
        pending: List[str] = []
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            update_id = entry.get("update_id") if isinstance(entry, dict) else None
            if not isinstance(update_id, int) or update_id <= cursor:
                completed.append(line)
            else:
                pending.append(line)
        retained = completed[-INBOX_CAP:] + pending
        try:
            self.inbox_path.write_text(
                "\n".join(retained) + ("\n" if retained else ""), encoding="utf-8"
            )
            if os.name == "posix":
                os.chmod(self.inbox_path, 0o600)
        except OSError:
            return

    def _plugin_cursor(self) -> int:
        cursor_path = self.inbox_path.with_name("plugin.cursor")
        try:
            value = int(cursor_path.read_text(encoding="utf-8").strip())
            return max(0, value)
        except (OSError, ValueError):
            return 0

    def _pending_inbox_count(self) -> int:
        if not self.inbox_path.is_file():
            return 0
        cursor = self._plugin_cursor()
        count = 0
        try:
            lines = self.inbox_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return INBOX_CAP
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            update_id = entry.get("update_id") if isinstance(entry, dict) else None
            if isinstance(update_id, int) and update_id > cursor:
                count += 1
        return count
