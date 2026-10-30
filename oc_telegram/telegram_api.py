"""Minimal Telegram Bot API client (standard library only)."""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

API_BASE = "https://api.telegram.org"
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_URL_OPENER = urllib.request.build_opener(_NoRedirect())


def _open_no_redirect(request, timeout):
    return _URL_OPENER.open(request, timeout=timeout)


def _safe_upload_filename(filename: str) -> str:
    """Remove multipart header delimiters and control characters from a name."""
    return "".join(
        "_" if char in {'\\', '"'} or ord(char) < 0x20 or ord(char) == 0x7F else char
        for char in filename
    )


class TelegramError(RuntimeError):
    """A Telegram API call failed."""

    def __init__(self, message: str, *, description: str = "", error_code: int = 0):
        super().__init__(message)
        self.description = description
        self.error_code = error_code


class TelegramClient:
    """Small, dependency-free wrapper over the Bot API endpoints we need."""

    def __init__(self, token: str, timeout: int = 30) -> None:
        if not token:
            raise ValueError("A bot token is required.")
        self._token = token
        self._timeout = timeout

    # -- internals ----------------------------------------------------------

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self._token}/{method}"

    def _call(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url(method), data=body, method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with _open_no_redirect(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise TelegramError(
                f"Telegram API {method} failed with HTTP {exc.code}",
                description=detail[:400],
                error_code=exc.code,
            ) from None
        except urllib.error.URLError as exc:
            raise TelegramError(
                f"Telegram API {method} failed: {exc.reason}"
            ) from None
        except OSError as exc:
            # socket.timeout, ssl.SSLError, ConnectionResetError and friends
            # are not URLErrors; without this they escape as a bare exception
            # and kill the long-running bridge on a transient network blip.
            raise TelegramError(
                f"Telegram API {method} failed (connection error): {exc}"
            ) from None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise TelegramError(f"Telegram API {method} returned non-JSON.")

        if not data.get("ok"):
            raise TelegramError(
                f"Telegram API {method} rejected the request.",
                description=str(data.get("description", ""))[:400],
                error_code=int(data.get("error_code", 0)),
            )
        return data

    def _call_get(
        self, method: str, params: Dict[str, Any], timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        query = urllib.parse.urlencode(params)
        url = f"{self._url(method)}?{query}"
        try:
            with _open_no_redirect(
                url, timeout=timeout or self._timeout
            ) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 409:
                raise TelegramError(
                    "Another process is already polling this bot (HTTP 409). "
                    "Only one getUpdates consumer is allowed per bot token.",
                    description=detail[:400],
                    error_code=409,
                ) from None
            raise TelegramError(
                f"Telegram API {method} failed with HTTP {exc.code}",
                description=detail[:400],
                error_code=exc.code,
            ) from None
        except urllib.error.URLError as exc:
            raise TelegramError(f"Telegram API {method} failed: {exc.reason}") from None
        except OSError as exc:
            raise TelegramError(
                f"Telegram API {method} failed (connection error): {exc}"
            ) from None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise TelegramError(f"Telegram API {method} returned non-JSON.")

        if not data.get("ok"):
            raise TelegramError(
                f"Telegram API {method} rejected the request.",
                description=str(data.get("description", ""))[:400],
                error_code=int(data.get("error_code", 0)),
            )
        return data

    # -- public API ---------------------------------------------------------

    def get_me(self) -> Dict[str, Any]:
        return self._call_get("getMe", {}).get("result", {})

    def send_message(self, chat_id: int, text: str, *, markdown: bool = False) -> Dict[str, Any]:
        if not text:
            raise TelegramError("Refusing to send an empty message.")
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if markdown:
            payload["parse_mode"] = "Markdown"
        return self._call("sendMessage", payload).get("result", {})

    def send_document(self, chat_id: int, path: Path, caption: str = "") -> Dict[str, Any]:
        """Upload a file using multipart/form-data."""
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise TelegramError("Document exceeds the 50 MiB upload limit.")
        boundary = uuid.uuid4().hex
        filename = _safe_upload_filename(path.name)
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        data = path.read_bytes()
        if len(data) > MAX_DOCUMENT_BYTES:
            raise TelegramError("Document exceeds the 50 MiB upload limit.")

        parts: List[bytes] = []
        for key, value in (
            ("chat_id", str(chat_id)),
            ("caption", caption),
        ):
            if not value:
                continue
            parts.append(
                f"--{boundary}\r\n".encode()
                + f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
                + value.encode("utf-8")
                + b"\r\n"
            )
        parts.append(
            f"--{boundary}\r\n".encode()
            + f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode()
            + f"Content-Type: {mime}\r\n\r\n".encode()
            + data
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())

        body = b"".join(parts)
        req = urllib.request.Request(
            self._url("sendDocument"), data=body, method="POST"
        )
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Content-Length", str(len(body)))
        try:
            with _open_no_redirect(req, timeout=max(self._timeout, 120)) as resp:
                payload_out = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise TelegramError(
                "Telegram API sendDocument failed.",
                description=detail[:400],
                error_code=exc.code,
            ) from None
        except urllib.error.URLError as exc:
            raise TelegramError(f"Telegram API sendDocument failed: {exc.reason}") from None
        except OSError as exc:
            raise TelegramError(f"Telegram API sendDocument failed: {exc}") from None

        if not payload_out.get("ok"):
            raise TelegramError(
                "Telegram API sendDocument rejected the request.",
                description=str(payload_out.get("description", ""))[:400],
            )
        return payload_out.get("result", {})

    def get_updates(self, offset: Optional[int], timeout: int = 30) -> List[Dict[str, Any]]:
        """Long-poll for updates. Returns an empty list on timeout.

        The HTTP read timeout must exceed the server-side long-poll timeout,
        otherwise the socket read aborts before Telegram answers.
        """
        params: Dict[str, Any] = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        try:
            data = self._call_get(
                "getUpdates", params, timeout=timeout + 15
            )
        except TelegramError as exc:
            # A long-poll timeout surfaces as a gateway error; treat it as
            # "no updates" rather than a hard failure.
            if exc.error_code in (502, 504):
                return []
            raise
        return data.get("result", []) or []

    def set_webhook(self, url: str = "") -> None:
        """Remove any webhook so long polling can be used."""
        self._call_get("deleteWebhook", {"drop_pending_updates": "false"})
