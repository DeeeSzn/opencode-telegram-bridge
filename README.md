# OpenCode ↔ Telegram Bridge

A small, local bridge that lets one allowlisted Telegram chat exchange messages with a live OpenCode session.

## Architecture

The integration has three separate pieces:

1. **Telegram poller** (`bridge.py`) is the only process that calls Telegram `getUpdates`. It enforces the chat allowlist, sanitizes input, persists the Telegram update cursor, and appends accepted messages to a private local inbox.
2. **OpenCode global plugin** (`plugins/telegram-inbox.js`) consumes that inbox, sends prompts to the selected/bound session through OpenCode's SDK, and sends the completed assistant response to Telegram.
3. **Telegram MCP server** (`mcp_server.py`) exposes explicit agent-to-Telegram tools: send a message, read recent inbound messages, send a permitted document, and inspect session bindings. It does not poll Telegram.

The OpenCode TUI owns the local HTTP server. The bridge connects to it on loopback, by default `http://127.0.0.1:4096`. Keep one TUI/server on that port.

## Requirements

- Python 3.9 or newer; runtime and unit tests use only the Python standard library.
- OpenCode installed and configured to listen on `127.0.0.1:4096`.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).

## Install

Clone to the default path so the global plugin can find `.env` automatically:

```sh
git clone https://github.com/<your-account>/opencode-telegram-bridge.git ~/opencode_telegram_bridge
cd ~/opencode_telegram_bridge
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set the bot token. Message the bot, then—before starting the bridge—run:

```sh
python3 scripts/get_chat_id.py
```

Copy the printed ID into both `TELEGRAM_DEFAULT_CHAT_ID` and
`TELEGRAM_ALLOWED_CHAT_IDS`. The helper reads the token from `.env`; it does not
place the token in shell history or print it. **The default chat must also be
listed in the explicit allowlist; an empty allowlist denies all chats.** Do
not put the bot token in shell startup files or OpenCode config.

For a non-default checkout location, set the non-secret path variable before starting OpenCode and installing the service:

```sh
export OC_TELEGRAM_ENV_FILE="$HOME/path/to/checkout/.env"
```

### Install the bridge service and plugin

macOS:

```sh
./install.sh --macos
```

Linux:

```sh
./install.sh --linux
```

These commands install the global OpenCode inbox plugin and register the Telegram poller as a user service. On macOS, the poller is a LaunchAgent; on Linux, it is a systemd user service. Restart OpenCode after installation so it loads the plugin.

The installer prints the MCP registration snippet. Add it to `~/.config/opencode/opencode.jsonc` using the paths it prints:

```jsonc
{
  "mcp": {
    "telegram-bridge": {
      "type": "local",
      "command": ["/usr/bin/python3", "/absolute/path/to/opencode_telegram_bridge/mcp_server.py"],
      "enabled": true
    }
  }
}
```

The MCP server reads the same package `.env`; no token needs to be stored in `opencode.jsonc`. Restart OpenCode after changing its config or plugin.

Start OpenCode with the loopback server pinned to the bridge port:

```sh
opencode --port 4096 --hostname 127.0.0.1
```

The service does not start or expose an OpenCode server itself. Keep the TUI/server running while you want Telegram to reach its session.

## Session selection

- By default, one allowlisted Telegram chat follows the current OpenCode session, then falls back to the most recently updated session.
- Use `/sessions` to list available sessions and `/bind <session_id>` to pin the Telegram chat to one session. `/unbind` returns to automatic selection.
- When multiple chats are allowlisted, each must be explicitly bound before the plugin will send its messages into an OpenCode session. This prevents sharing one session's context across Telegram users.

## Telegram commands

| Command | Effect |
|---|---|
| `/start`, `/help` | Show help |
| `/status` | Show the bound session and model |
| `/sessions` | List recent sessions |
| `/bind <session_id>` | Pin this chat to a session |
| `/unbind` | Remove the pin |
| `/stop` | Interrupt the bound/selected session |
| `/approve`, `/reject` | Not available in this release; handle permission prompts in the OpenCode UI |

Telegram-originated prompts use the selected session's existing model, agent, tools, and permissions. Telegram cannot grant OpenCode permissions in this release.

## MCP tools

- `telegram_send_message` — send an intentional/proactive message.
- `telegram_read_messages` — read recent allowlisted inbound messages from the local inbox.
- `telegram_send_document` — send a file under the configured media root (50 MiB maximum).
- `telegram_session_info` — inspect saved chat/session bindings and OpenCode health.

The plugin automatically sends the final response to Telegram for a Telegram-originated turn. Use `telegram_send_message` for separate notifications rather than echoing that same final response a second time.

## Configuration

See `.env.example`. Important settings:

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Required bot token |
| `TELEGRAM_DEFAULT_CHAT_ID` | — | Destination when an MCP tool omits `chat_id` |
| `TELEGRAM_ALLOWED_CHAT_IDS` | empty | Explicit comma-separated allowlist; empty denies all |
| `OPENCODE_URL` | `http://127.0.0.1:4096` | OpenCode loopback API |
| `OPENCODE_SERVER_USERNAME` | `opencode` | Basic-auth username if configured on the server |
| `OPENCODE_SERVER_PASSWORD` | empty | Basic-auth password if configured |
| `OC_TELEGRAM_STATE_DIR` | `~/.opencode_telegram` | Private local state and message inbox directory |
| `OC_TELEGRAM_ENV_FILE` | package `.env` | Optional path to the shared environment file |
| `OC_TELEGRAM_RESPONSE_TIMEOUT` | `600` | Agent response timeout in seconds |
| `OC_TELEGRAM_MEDIA_ROOT` | unset | Required dedicated directory allowed for document uploads |

## Security notes

- Telegram messages are untrusted prompts. They operate with the OpenCode session's existing permissions; do not use a broadly privileged session for an untrusted chat.
- `TELEGRAM_ALLOWED_CHAT_IDS` is explicit and deny-by-default. Chat IDs are checked before inbox writes or tool calls.
- Every allowlisted chat is a trusted operator of the same OpenCode instance and can list/select its sessions. Do not use a shared bot as a multi-tenant boundary for unrelated users.
- OpenCode URLs must use HTTP(S) loopback hosts. Do not expose the unauthenticated OpenCode server to the network.
- `.env` is ignored by Git and the installer restricts it to owner read/write on POSIX systems. The inbox, cursor, and state files are also private.
- Telegram's bot token is required in the HTTPS Bot API URL. The code avoids logging raw request exceptions that could include that URL.
- Only the bridge daemon polls `getUpdates`; starting a second poller with the same bot token causes HTTP 409 conflicts.
- File uploads are resolved through symlinks, constrained to configured roots, and limited to 50 MiB.
- Document upload is disabled unless a dedicated media root is configured. Credential-like files, hidden environment files, and application state files are refused even inside that root.
- The inbox cursor is based on Telegram update IDs. Repeated updates are deduplicated in the retained inbox window.

For vulnerability reports, use GitHub's private security advisory reporting for this repository rather than opening a public issue containing secrets or exploit details.

## Verify

Offline unit tests:

```sh
python3 tests/test_bridge.py
```

MCP framing/dispatch probe (requires `.env`; it refuses outbound Telegram sends and file escapes):

```sh
python3 tests/probe_mcp.py
```

Service status:

```sh
# macOS
launchctl list local.opencode-telegram-bridge

# Linux
systemctl --user status opencode-telegram-bridge
```

Logs are stored under `~/.opencode_telegram/`. They avoid message bodies and bot-token values.

## License

MIT. See [LICENSE](LICENSE).
