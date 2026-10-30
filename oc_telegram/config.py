"""Environment-backed configuration with validation and secret redaction.

All tunables are read from the environment (optionally via a .env file).
Nothing is hardcoded, so the same package works for any user.
"""

from __future__ import annotations

import os
import re
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

# --- .env loading (stdlib only) ---------------------------------------------

_ENV_LINE = re.compile(
    r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$""",
    re.MULTILINE,
)
_TELEGRAM_TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")

# The directory that contains this package. Secrets are loaded from the
# package's .env or one explicitly selected by OC_TELEGRAM_ENV_FILE; the
# process working directory is not searched for unrelated project secrets.
_PACKAGE_DIR = Path(__file__).resolve().parent.parent


def _env_candidates() -> list[Path]:
    configured = os.getenv("OC_TELEGRAM_ENV_FILE", "").strip()
    if configured:
        return [Path(configured).expanduser()]
    return [_PACKAGE_DIR / ".env"]


def load_dotenv(path: Optional[Path] = None) -> None:
    """Populate os.environ from a .env file.

    Existing values win, *except* empty strings: OpenCode's config
    substitution turns an unset `{env:VAR}` into "", and an empty value must
    not shadow the .env file.
    """
    candidates = [path] if path is not None else _env_candidates()
    for env_path in candidates:
        if not env_path or not env_path.is_file():
            continue
        try:
            raw = env_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for key, value in _ENV_LINE.findall(raw):
            if key in os.environ and os.environ[key] != "":
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ[key] = value
        return


# --- helpers ----------------------------------------------------------------


def _int(name: str, default: int, minimum: int = 0, maximum: int = 2**31) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _csv_ints(name: str) -> tuple[int, ...]:
    raw = os.getenv(name, "")
    out: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            continue
    return tuple(dict.fromkeys(out))


# --- configuration ----------------------------------------------------------


@dataclass(frozen=True)
class Config:
    # Telegram
    bot_token: str
    default_chat_id: Optional[int]
    allowed_chat_ids: tuple[int, ...]

    # OpenCode
    opencode_url: str
    opencode_username: str
    opencode_password: str
    opencode_timeout: int

    # Behaviour
    session_dir: Path
    state_file: Path
    rate_limit_count: int
    rate_limit_window: int
    max_message_length: int
    max_reply_length: int
    response_timeout: int
    poll_timeout: int
    media_root: Optional[Path]
    log_level: str

    # Secrets that must never appear in logs or tool output.
    secrets: tuple[str, ...] = field(default=(), repr=False)

    @property
    def strict(self) -> bool:
        """Deny-by-default mode: only chats in `allowed_chat_ids` may interact."""
        return bool(self.allowed_chat_ids)

    def redact(self, text: str) -> str:
        """Remove secrets from any string that may be surfaced to a human."""
        if not text:
            return text
        for secret in self.secrets:
            if secret and len(secret) >= 6 and secret in text:
                text = text.replace(secret, "***REDACTED***")
        return text


class ConfigError(RuntimeError):
    """Raised when the environment is missing required configuration."""


def load_config(require_token: bool = True) -> Config:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if require_token and not _TELEGRAM_TOKEN.fullmatch(token):
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN is missing or malformed. Configure it in the "
            "environment or in the file selected by OC_TELEGRAM_ENV_FILE; "
            "the token value is intentionally not shown."
        )

    default_chat_raw = os.getenv("TELEGRAM_DEFAULT_CHAT_ID", "").strip()
    default_chat_id: Optional[int] = None
    if default_chat_raw:
        try:
            default_chat_id = int(default_chat_raw)
        except ValueError:
            default_chat_id = None

    allowed = _csv_ints("TELEGRAM_ALLOWED_CHAT_IDS")

    url = os.getenv("OPENCODE_URL", "http://127.0.0.1:4096").strip().rstrip("/")
    # The OpenCode server has no authentication by default. Require an HTTP(S)
    # loopback URL; HTTPS must not be an escape hatch for remote hosts.
    try:
        parsed_url = urlsplit(url)
        hostname = parsed_url.hostname
        parsed_url.port  # Force validation of malformed ports.
    except ValueError as exc:
        raise ConfigError("OPENCODE_URL is malformed.") from exc

    try:
        is_loopback = bool(hostname) and ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = (hostname or "").lower() == "localhost"

    if (
        parsed_url.scheme not in {"http", "https"}
        or not is_loopback
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ConfigError(
            "Refusing OPENCODE_URL: use an HTTP(S) loopback URL without "
            "credentials, query, or fragment. The OpenCode server is "
            "unauthenticated by default."
        )

    state_dir = Path(
        os.getenv("OC_TELEGRAM_STATE_DIR", str(Path.home() / ".opencode_telegram"))
    ).expanduser()

    media_root_raw = os.getenv("OC_TELEGRAM_MEDIA_ROOT", "").strip()
    media_root = Path(media_root_raw).expanduser() if media_root_raw else None

    password = os.getenv("OPENCODE_SERVER_PASSWORD", "")

    secrets = tuple(s for s in (token, password) if s)

    return Config(
        bot_token=token,
        default_chat_id=default_chat_id,
        allowed_chat_ids=allowed,
        opencode_url=url,
        opencode_username=os.getenv("OPENCODE_SERVER_USERNAME", "opencode").strip(),
        opencode_password=password,
        opencode_timeout=_int("OPENCODE_TIMEOUT", 30, 1, 600),
        session_dir=state_dir,
        state_file=state_dir / "state.json",
        rate_limit_count=_int("TELEGRAM_RATE_LIMIT_COUNT", 20, 1, 10_000),
        rate_limit_window=_int("TELEGRAM_RATE_LIMIT_WINDOW", 60, 1, 86_400),
        max_message_length=_int("TELEGRAM_MAX_MESSAGE_LENGTH", 8000, 1, 100_000),
        max_reply_length=_int("TELEGRAM_MAX_REPLY_LENGTH", 4000, 100, 4096),
        response_timeout=_int("OC_TELEGRAM_RESPONSE_TIMEOUT", 600, 10, 3600),
        poll_timeout=_int("TELEGRAM_POLL_TIMEOUT", 30, 0, 50),
        media_root=media_root,
        log_level=os.getenv("OC_TELEGRAM_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        secrets=secrets,
    )
