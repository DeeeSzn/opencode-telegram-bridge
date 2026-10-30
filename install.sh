#!/usr/bin/env bash
# Install the OpenCode <-> Telegram bridge as a background service.
#
#   ./install.sh --macos     launchd (macOS)
#   ./install.sh --linux     systemd --user (Linux)
#   ./install.sh --check     verify the environment
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"
PLATFORM="$(uname -s)"
LABEL="opencode-telegram-bridge"
STATE_DIR="${OC_TELEGRAM_STATE_DIR:-$HOME/.opencode_telegram}"
ENV_FILE="${OC_TELEGRAM_ENV_FILE:-$HERE/.env}"
OPENCODE_BIN="$(command -v opencode || true)"
PORT="${OPENCODE_PORT:-4096}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

check_env() {
  log "checking environment"

  command -v python3 >/dev/null 2>&1 || die "python3 not found"
  "$PYTHON" - <<'PY' || die "Python 3.9+ required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY

  if [ ! -f "$ENV_FILE" ]; then
    warn "environment file not found - copy .env.example to .env and fill it in"
  else
    chmod 600 "$ENV_FILE" || die "could not restrict permissions on the environment file"
    if ! OC_TELEGRAM_ENV_FILE="$ENV_FILE" "$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); from oc_telegram.config import load_config; cfg=load_config(require_token=True); raise SystemExit(0 if cfg.allowed_chat_ids else 1)' "$HERE" >/dev/null 2>&1; then
      die "invalid bridge configuration or empty chat allowlist; check .env without sharing its contents"
    fi
    log "TELEGRAM_BOT_TOKEN is set"
  fi

  [ -n "$OPENCODE_BIN" ] || warn "opencode CLI not found in PATH"

  if command -v curl >/dev/null 2>&1; then
    if curl -fsS "http://127.0.0.1:$PORT/global/health" >/dev/null 2>&1; then
      log "OpenCode server is already running on port $PORT"
    else
      warn "no OpenCode server on port $PORT (it will be started by the service)"
    fi
  fi
}

require_service_config() {
  [ -f "$ENV_FILE" ] || die "create $ENV_FILE before installing the service"
  OC_TELEGRAM_ENV_FILE="$ENV_FILE" "$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); from oc_telegram.config import load_config; cfg=load_config(require_token=True); raise SystemExit(0 if cfg.allowed_chat_ids else 1)' "$HERE" >/dev/null 2>&1 || die "invalid bridge configuration or empty chat allowlist; check the environment file without sharing its contents"
  chmod 600 "$ENV_FILE" || die "could not restrict permissions on the environment file"
}

install_opencode_plugin() {
  local plugin_dir="$HOME/.config/opencode/plugins"
  local plugin_path="$plugin_dir/telegram-inbox.js"
  local source="$HERE/plugins/telegram-inbox.js"

  mkdir -p "$plugin_dir"
  if [ -e "$plugin_path" ] && cmp -s "$source" "$plugin_path"; then
    log "OpenCode inbox plugin is already current"
    return
  fi
  if [ -e "$plugin_path" ] || [ -L "$plugin_path" ]; then
    local backup="$plugin_path.backup.$(date +%Y%m%d%H%M%S)"
    cp -p "$plugin_path" "$backup" || die "could not back up the existing OpenCode plugin"
    log "backed up existing plugin to $backup"
  fi
  cp "$source" "$plugin_path" || die "could not install the OpenCode inbox plugin"
  chmod 644 "$plugin_path"
  log "installed OpenCode inbox plugin at $plugin_path"
}

write_opencode_config_hint() {
  log "add this to your OpenCode config (~/.config/opencode/opencode.jsonc):"
  cat <<EOF

  "telegram-bridge": {
    "type": "local",
    "command": ["$PYTHON", "$HERE/mcp_server.py"],
    "enabled": true
  }
EOF
}

install_macos() {
  command -v launchctl >/dev/null 2>&1 || die "launchctl not found"
  require_service_config
  install_opencode_plugin
  mkdir -p "$STATE_DIR"

  local bridge_plist="$HOME/Library/LaunchAgents/local.$LABEL.plist"

  # plistlib handles XML escaping for paths instead of interpolating them
  # into a shell-generated XML document.
  "$PYTHON" - "$bridge_plist" "$PYTHON" "$HERE" "$HOME" "$STATE_DIR" <<'PY'
import os
import plistlib
import sys

plist_path, python, here, home, state_dir = sys.argv[1:]
payload = {
    "Label": "local.opencode-telegram-bridge",
    "ProgramArguments": [python, os.path.join(here, "bridge.py")],
    "WorkingDirectory": here,
    "EnvironmentVariables": {
        "HOME": home,
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        **{
            key: os.environ[key]
            for key in ("OC_TELEGRAM_ENV_FILE", "OC_TELEGRAM_STATE_DIR")
            if os.environ.get(key)
        },
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": os.path.join(state_dir, "bridge.log"),
    "StandardErrorPath": os.path.join(state_dir, "bridge.err.log"),
}
with open(plist_path, "wb") as output:
    plistlib.dump(payload, output, fmt=plistlib.FMT_XML, sort_keys=True)
os.chmod(plist_path, 0o644)
PY
  log "wrote $bridge_plist"

  # No separate `opencode serve` is started. The bridge targets the server the
  # TUI itself embeds, so run the TUI pinned to the same port (the `opencode()`
  # function in ~/.zshrc adds this automatically):
  #     opencode --port $PORT --hostname 127.0.0.1
  log "run opencode with: --port $PORT --hostname 127.0.0.1"

  local domain="gui/$(id -u)"
  local service="$domain/local.$LABEL"
  launchctl bootout "$service" 2>/dev/null || true
  for attempt in {1..50}; do
    if ! launchctl print "$service" >/dev/null 2>&1; then
      break
    fi
    sleep 0.2
  done
  sleep 1
  launchctl bootstrap "$domain" "$bridge_plist" || die "could not bootstrap the bridge agent"
  log "loaded local.$LABEL"
}

install_linux() {
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found"
  require_service_config
  install_opencode_plugin
  local unit_dir="$HOME/.config/systemd/user"

  mkdir -p "$unit_dir" "$STATE_DIR"

  cat > "$unit_dir/$LABEL.service" <<EOF
[Unit]
Description=OpenCode Telegram bridge
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$HERE
EnvironmentFile=-$ENV_FILE
Environment=OC_TELEGRAM_ENV_FILE=$ENV_FILE
Environment=OC_TELEGRAM_STATE_DIR=$STATE_DIR
ExecStart=$PYTHON $HERE/bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  log "wrote $unit_dir/$LABEL.service"

  systemctl --user daemon-reload
  systemctl --user enable --now "$LABEL.service"
  log "enabled $LABEL.service"
}

case "${1:-}" in
  --macos)  check_env; install_macos; write_opencode_config_hint ;;
  --linux)  check_env; install_linux; write_opencode_config_hint ;;
  --check)  check_env ;;
  *)        echo "usage: $0 [--macos|--linux|--check]"; exit 1 ;;
esac

log "done"
