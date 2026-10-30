"""Safety controls for the Telegram <-> OpenCode bridge.

Everything that crosses the trust boundary (Telegram -> agent) passes through
here first. The design is deny-by-default and treats Telegram input as
untrusted: it can steer the agent, but it can never widen its own authority.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, Iterable, Optional

# OpenCode session ids look like `ses_<base62ish>`. Validating before use
# keeps untrusted input out of URL paths.
SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]{8,64}$")

# Control characters that have no business in a prompt (keep \n and \t).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Zero-width and bidi-override characters are a classic prompt-injection
# vector because they are invisible in most renderers.
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")

# Telegram's Markdown parser is easy to break and easy to abuse for spoofing.
# We escape instead of trusting caller-supplied parse modes.
_MD_SPECIALS = r"_*[]()~`>#+-=|{}.!"


class PolicyError(PermissionError):
    """Raised when an action is refused by policy."""


def sanitize_text(text: str, max_length: int = 8000) -> str:
    """Normalise untrusted text before it reaches the agent."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = _INVISIBLE_RE.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = text.strip()
    if max_length > 0 and len(text) > max_length:
        text = text[:max_length]
    return text


def escape_markdown(text: str) -> str:
    """Escape Markdown control characters so user text cannot spoof formatting."""
    if not text:
        return ""
    return re.sub(f"([{re.escape(_MD_SPECIALS)}])", r"\\\1", text)


def validate_session_id(session_id: str) -> str:
    """Return the session id if it is well formed, else raise PolicyError."""
    if not session_id or not SESSION_ID_RE.match(session_id):
        raise PolicyError(f"Malformed session id: {session_id!r}")
    return session_id


def validate_chat_id(chat_id: object, allowed: Iterable[int]) -> int:
    """Validate a chat id and confirm it is on the allowlist.

    Deny-by-default: an empty allowlist rejects every chat. There is no
    "allow everyone" mode, because this value controls who can drive the
    agent.
    """
    if isinstance(chat_id, bool):
        raise PolicyError(f"Invalid chat id: {chat_id!r}")
    if isinstance(chat_id, int):
        value = chat_id
    elif isinstance(chat_id, str) and re.fullmatch(r"[0-9]+", chat_id.strip()):
        value = int(chat_id.strip())
    else:
        raise PolicyError(f"Invalid chat id: {chat_id!r}")
    if value <= 0:
        raise PolicyError("Chat id must be a positive integer.")
    allowlist = set(allowed)
    if not allowlist:
        raise PolicyError(
            "No chat is allowed: TELEGRAM_ALLOWED_CHAT_IDS is empty (deny-by-default)."
        )
    if value not in allowlist:
        raise PolicyError(f"Chat {value} is not on the allowlist.")
    return value


def resolve_media_path(
    raw_path: str, media_root: Optional[Path], allowed_roots: Iterable[Path]
) -> Path:
    """Resolve a media path, refusing anything outside the permitted roots.

    Symlinks are resolved before the containment check so a symlink cannot be
    used to read (or exfiltrate) files outside the sandbox.
    """
    if not raw_path:
        raise PolicyError("file_path is required.")

    candidate = Path(raw_path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise PolicyError(f"file_path does not exist or is not readable: {raw_path!r}")

    if not resolved.is_file():
        raise PolicyError(f"file_path is not a regular file: {raw_path!r}")

    lower_name = resolved.name.lower()
    sensitive_names = {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "authorized_keys",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "inbox.jsonl",
        "known_hosts",
        "state.json",
    }
    sensitive_suffixes = {".key", ".keystore", ".p12", ".pem", ".pfx"}
    sensitive_directories = {".aws", ".gnupg", ".git", ".ssh"}
    if (
        lower_name in sensitive_names
        or lower_name == ".env"
        or lower_name.startswith(".env.")
        or Path(lower_name).suffix in sensitive_suffixes
        or any(part.lower() in sensitive_directories for part in resolved.parts)
    ):
        raise PolicyError("This file type is not permitted for Telegram upload.")

    roots: list[Path] = []
    if media_root is not None:
        roots.append(media_root.expanduser())
    for root in allowed_roots:
        roots.append(Path(root).expanduser())

    if not roots:
        raise PolicyError("No permitted media roots are configured.")

    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            break
        except ValueError:
            continue
    else:
        allowed_list = ", ".join(str(r) for r in roots)
        raise PolicyError(
            f"file_path is outside the permitted roots ({allowed_list})."
        )

    return resolved


@dataclass
class RateLimiter:
    """Sliding-window rate limiter, keyed by chat id."""

    limit: int
    window: int
    _hits: Dict[int, Deque[float]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._hits = defaultdict(deque)

    def check(self, key: int, now: Optional[float] = None) -> bool:
        """Return True when the action is allowed, False when rate limited."""
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        cutoff = now - self.window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def retry_after(self, key: int, now: Optional[float] = None) -> int:
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        if not hits:
            return 0
        return max(1, int(self.window - (now - hits[0])) + 1)


@dataclass
class PermissionPolicy:
    """Decides how agent permission requests are surfaced to Telegram.

    The bridge never grants standing approval. A permission raised by an
    agent turn that was triggered from Telegram is answered only by an
    explicit instruction from the same chat, and only for that single action.
    """

    allow_once: bool = True

    ALLOWED_REPLIES = ("once", "reject")

    def classify(self, action: str, resources: Iterable[str]) -> str:
        """Bucket a permission request so the user sees a clear choice."""
        action_l = (action or "").lower()
        resources_l = [r.lower() for r in resources or []]
        dangerous = (
            any(r.startswith(("rm ", "rm -", "sudo ", "curl ", "wget ")) for r in resources_l)
            or "shell" in action_l
            or "bash" in action_l
            or "execute" in action_l
        )
        return "dangerous" if dangerous else "normal"

    def validate_reply(self, reply: str) -> str:
        """Validate a reply coming from Telegram. `always` is never allowed."""
        value = (reply or "").strip().lower()
        if value not in self.ALLOWED_REPLIES:
            raise PolicyError(
                f"Unsupported reply {reply!r}. Use 'once' or 'reject' "
                "(standing approval is never granted from Telegram)."
            )
        if value == "once" and not self.allow_once:
            raise PolicyError("One-time approval is disabled by policy.")
        return value
