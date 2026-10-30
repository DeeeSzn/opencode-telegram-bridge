"""Client for the OpenCode server HTTP API (standard library only).

The important capability here is `send_prompt`, which posts into a live
session exactly the way typing into the TUI does. Because the prompt is
delivered to an existing session, the session's own model, agent, tools and
history are used - no separate process, no separate model choice.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

from .guardrails import validate_session_id


class OpenCodeError(RuntimeError):
    """The OpenCode server returned an error."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_URL_OPENER = urllib.request.build_opener(_NoRedirect())


class OpenCodeClient:
    def __init__(
        self,
        base_url: str,
        username: str = "opencode",
        password: str = "",
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout

    # -- internals ----------------------------------------------------------

    def _auth_header(self) -> Optional[str]:
        if not self.password:
            return None
        import base64

        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _URL_OPENER.open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                exc.close()
            except OSError:
                pass
            raise OpenCodeError(f"OpenCode request failed with HTTP {status}.") from None
        except urllib.error.URLError:
            raise OpenCodeError("OpenCode request failed; the local server is unavailable.") from None

        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise OpenCodeError(f"{method} {path} returned non-JSON.")

    # -- sessions -----------------------------------------------------------

    def list_sessions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """List sessions, newest first."""
        data = self._request("GET", "/api/session")
        sessions = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(sessions, list):
            return []
        sessions.sort(
            key=lambda s: (s.get("time", {}) or {}).get("updated", 0), reverse=True
        )
        return sessions[:limit]

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        validate_session_id(session_id)
        data = self._request("GET", f"/api/session/{session_id}")
        if isinstance(data, dict) and "data" in data:
            return data.get("data") or None
        return data if isinstance(data, dict) else None

    def active_session(self) -> Optional[Dict[str, Any]]:
        """Return a session the server considers active, if any.

        GET /api/session/active responds with a *record* (map) of
        sessionID -> session info, not a single session, so the map has to
        be unwrapped here. The most recently updated entry wins.

        Newer OpenCode servers no longer expose this endpoint (it answers
        ``UnknownError``), so any failure is swallowed and the caller falls
        back to the most recently updated session from ``list_sessions``.
        """
        try:
            data = self._request("GET", "/api/session/active")
        except OpenCodeError:
            return None
        if not isinstance(data, dict):
            return None
        payload = data.get("data")
        if not payload:
            return None
        if isinstance(payload, dict):
            # Either a map of sessionID -> info, or a single session object.
            if "id" in payload:
                return payload
            entries = [v for v in payload.values() if isinstance(v, dict) and v.get("id")]
            if not entries:
                return None
            entries.sort(
                key=lambda s: (s.get("time", {}) or {}).get("updated", 0), reverse=True
            )
            return entries[0]
        return None

    def resolve_session(self, preferred: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Pick the session a Telegram message should be delivered to.

        Preference order:
          1. an explicitly bound session id (validated),
          2. the server's active session,
          3. the most recently updated session.
        """
        if preferred:
            session = self.get_session(preferred)
            if session:
                return session
            raise OpenCodeError(f"Session {preferred!r} was not found.")

        active = self.active_session()
        if active and active.get("id"):
            return active

        sessions = self.list_sessions(limit=1)
        return sessions[0] if sessions else None

    def session_model(self, session: Dict[str, Any]) -> str:
        model = session.get("model") or {}
        if not isinstance(model, dict):
            return "unknown"
        provider = model.get("providerID", "?")
        model_id = model.get("id", "?")
        variant = model.get("variant")
        suffix = f" ({variant})" if variant and variant != "default" else ""
        return f"{provider}/{model_id}{suffix}"

    # -- prompting ----------------------------------------------------------

    def send_prompt(
        self,
        session_id: str,
        text: str,
        delivery: str = "steer",
        agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Deliver text into a session as if the user had typed it.

        `delivery="steer"` interrupts the running turn and injects the text
        (like typing mid-run); `delivery="queue"` waits for the current turn.
        """
        validate_session_id(session_id)
        if delivery not in ("steer", "queue"):
            raise OpenCodeError(f"Invalid delivery mode: {delivery!r}")

        body: Dict[str, Any] = {
            "prompt": {"text": text},
            "delivery": delivery,
        }
        if agent:
            body["prompt"]["agents"] = [{"agent": agent}]

        data = self._request(
            "POST", f"/api/session/{session_id}/prompt", body, timeout=self.timeout
        )
        return (data or {}).get("data", {}) if isinstance(data, dict) else {}

    def messages(self, session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        validate_session_id(session_id)
        data = self._request("GET", f"/api/session/{session_id}/message")
        if isinstance(data, dict):
            items = data.get("data", []) or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        return items[-limit:] if limit else items

    # -- v1 execution surface ------------------------------------------------
    #
    # The v2 endpoint (/api/session/{id}/prompt) only *admits* an input
    # durably; a client that owns the session drain has to promote it. The v1
    # endpoint (/session/{id}/prompt_async) is executed by the server itself,
    # using the session's own model, which is what the bridge needs.

    def prompt_async(
        self,
        session_id: str,
        text: str,
        agent: Optional[str] = None,
    ) -> None:
        """Queue a message for the agent and return immediately.

        The server runs the turn with the session's configured model, agent
        and history, so the message behaves exactly like a typed prompt.
        """
        validate_session_id(session_id)
        body: Dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if agent:
            body["agent"] = agent
        self._request("POST", f"/session/{session_id}/prompt_async", body)

    def session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Return the session's messages normalised for the bridge.

        The v1 endpoint answers with a list of ``{info, parts}`` objects:
        ``info.role`` carries the role and ``parts`` carries the content.
        """
        validate_session_id(session_id)
        data = self._request("GET", f"/session/{session_id}/message")
        items = data if isinstance(data, list) else (data or {}).get("data", []) or []
        normalised: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            info = item.get("info") or {}
            parts = item.get("parts") or []
            texts = [
                str(p.get("text", ""))
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
            ]
            tools = [
                {
                    "name": p.get("tool") or p.get("name") or "",
                    "status": (p.get("state") or {}).get("status", "")
                    if isinstance(p.get("state"), dict)
                    else "",
                }
                for p in parts
                if isinstance(p, dict) and p.get("type") == "tool"
            ]
            normalised.append(
                {
                    "id": info.get("id", ""),
                    "role": info.get("role", ""),
                    "created": (info.get("time") or {}).get("created"),
                    "completed": (info.get("time") or {}).get("completed"),
                    "error": (info.get("error") or {}).get("data", {}).get("message")
                    if isinstance(info.get("error"), dict)
                    else None,
                    "text": "\n".join(texts).strip(),
                    "tools": tools,
                }
            )
        return normalised

    def session_status(self) -> Dict[str, Any]:
        """Per-session error/retry status.

        The v1 message API does not expose a failed turn: a failed agent
        leaves an assistant message with no parts and no completion time.
        This endpoint reports the reason, for example::

            {"ses_x": {"type": "retry", "attempt": 1,
                       "message": "Free usage exceeded, subscribe to Go"}}
        """
        data = self._request("GET", "/session/status")
        return data if isinstance(data, dict) else {}

    def wait_for_reply(
        self,
        session_id: str,
        after_message_id: str = "",
        timeout: int = 300,
        poll: float = 2.0,
    ) -> Optional[Dict[str, Any]]:
        """Block until the agent finishes the turn.

        Returns the assistant message, or an assistant message carrying an
        ``error`` when the turn failed, or None on timeout.

        A failed turn leaves an assistant message with no parts, which is
        indistinguishable from a slow model that has not emitted its first
        token yet. The session status endpoint is the tie-breaker: it reports
        ``busy`` while the turn is running and a concrete reason (for
        example a provider quota error) once it has failed.
        """
        deadline = time.monotonic() + timeout
        empty_since: Optional[float] = None

        while time.monotonic() < deadline:
            assistant: Optional[Dict[str, Any]] = None
            for message in reversed(self.session_messages(session_id)):
                if message.get("role") != "assistant":
                    continue
                if after_message_id and message.get("id") == after_message_id:
                    break
                assistant = message
                break

            if assistant is not None and assistant.get("completed"):
                return assistant

            if assistant is not None and not assistant.get("text"):
                now = time.monotonic()
                empty_since = now if empty_since is None else empty_since
                status = self.session_status().get(session_id)
                if isinstance(status, dict):
                    kind = str(status.get("type", ""))
                    if kind and kind != "busy":
                        reason = status.get("message") or (
                            (status.get("action") or {}).get("message")
                        )
                        return {
                            **assistant,
                            "error": reason or f"the agent failed ({kind})",
                        }
                elif empty_since is not None and now - empty_since > 120:
                    return {**assistant, "error": "the agent produced no output"}
            else:
                empty_since = None

            time.sleep(poll)
        return None

    def last_assistant_message(self, session_id: str) -> Optional[Dict[str, Any]]:
        for message in reversed(self.session_messages(session_id)):
            if message.get("role") == "assistant":
                return message
        return None

    def wait_for_idle(self, session_id: str, timeout: int = 600) -> bool:
        """Block until the session has no running turn."""
        validate_session_id(session_id)
        try:
            self._request(
                "POST", f"/api/session/{session_id}/wait", {}, timeout=timeout
            )
            return True
        except OpenCodeError:
            return False

    # -- events -------------------------------------------------------------

    def session_events(
        self, session_id: str, timeout: int = 30
    ) -> Iterator[Dict[str, Any]]:
        """Stream session events from GET /api/session/{id}/event.

        The endpoint is a Server-Sent Events stream, but it does not use the
        `event:` field. Every frame is a single `data:` line holding a JSON
        envelope::

            {"id":"evt_...","type":"session.next.text.delta",
             "durable":{"aggregateID":"ses_...","seq":3,"version":1},
             "data":{"assistantMessageID":"msg_...","textID":"txt_...","delta":"hi"}}

        The useful payload lives under the envelope's own ``data`` key, so
        each yielded dict is {"type": <envelope type>, "data": <payload>}.
        """
        validate_session_id(session_id)
        url = f"{self.base_url}/api/session/{session_id}/event"
        headers = {"Accept": "text/event-stream"}
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            resp = _URL_OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise OpenCodeError(
                f"event stream failed with HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise OpenCodeError(f"event stream failed: {exc.reason}") from exc

        data_lines: List[str] = []
        try:
            while True:
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(":"):
                    continue  # SSE comment / keep-alive
                if line == "":
                    # Frame boundary.
                    if data_lines:
                        blob = "\n".join(data_lines)
                        data_lines = []
                        try:
                            envelope = json.loads(blob)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(envelope, dict):
                            payload = envelope.get("data")
                            yield {
                                "id": envelope.get("id", ""),
                                "type": envelope.get("type", ""),
                                "data": payload if isinstance(payload, dict) else {},
                            }
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                # `event:`/`id:`/`retry:` fields are not used by this endpoint.
        finally:
            try:
                resp.close()
            except Exception:
                pass

    # -- permissions --------------------------------------------------------

    def pending_permissions(self, session_id: str) -> List[Dict[str, Any]]:
        validate_session_id(session_id)
        data = self._request("GET", f"/api/session/{session_id}/permission")
        if isinstance(data, dict):
            return data.get("data", []) or []
        return []

    def reply_permission(
        self, session_id: str, request_id: str, reply: str, message: str = ""
    ) -> Dict[str, Any]:
        """Answer a permission request. `reply` is once | always | reject."""
        validate_session_id(session_id)
        body: Dict[str, Any] = {"reply": reply}
        if message:
            body["message"] = message
        data = self._request(
            "POST",
            f"/api/session/{session_id}/permission/{request_id}/reply",
            body,
        )
        return (data or {}).get("data", {}) if isinstance(data, dict) else {}

    # -- server info --------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        data = self._request("GET", "/global/health")
        return data if isinstance(data, dict) else {}
