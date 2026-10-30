#!/usr/bin/env python3
"""Print chat IDs from pending bot updates without exposing the bot token."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oc_telegram import ConfigError, TelegramClient, TelegramError, load_config  # noqa: E402


def main() -> int:
    try:
        config = load_config(require_token=True)
        updates = TelegramClient(config.bot_token).get_updates(None, timeout=0)
    except (ConfigError, TelegramError) as exc:
        print(f"Could not read Telegram updates: {exc}", file=sys.stderr)
        return 1

    chat_ids = sorted(
        {
            str(message["chat"]["id"])
            for update in updates
            if isinstance((message := update.get("message")), dict)
            and isinstance(message.get("chat"), dict)
            and message["chat"].get("id") is not None
        }
    )
    if not chat_ids:
        print("No chat IDs found. Send the bot a message, then run this again.")
        return 0

    for chat_id in chat_ids:
        print(chat_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
