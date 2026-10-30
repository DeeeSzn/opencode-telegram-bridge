"""OpenCode <-> Telegram bridge package."""

from .config import Config, ConfigError, load_config, load_dotenv
from .guardrails import (
    PermissionPolicy,
    PolicyError,
    RateLimiter,
    escape_markdown,
    sanitize_text,
    validate_chat_id,
    validate_session_id,
    resolve_media_path,
)
from .opencode_api import OpenCodeClient, OpenCodeError
from .state import InboxFullError, StateStore
from .telegram_api import TelegramClient, TelegramError

__version__ = "1.0.0"

__all__ = [
    "Config",
    "ConfigError",
    "load_config",
    "load_dotenv",
    "PermissionPolicy",
    "PolicyError",
    "RateLimiter",
    "escape_markdown",
    "sanitize_text",
    "validate_chat_id",
    "validate_session_id",
    "resolve_media_path",
    "OpenCodeClient",
    "OpenCodeError",
    "StateStore",
    "InboxFullError",
    "TelegramClient",
    "TelegramError",
]
