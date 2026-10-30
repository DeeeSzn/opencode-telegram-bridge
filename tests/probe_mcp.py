#!/usr/bin/env python3
"""Drive the MCP server exactly like OpenCode's MCP SDK does.

Speaks newline-delimited JSON-RPC over stdio, sends initialize -> tools/list
-> tools/call, and verifies each response.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_server.py"
sys.path.insert(0, str(ROOT))

from oc_telegram import load_config  # noqa: E402

env = dict(os.environ)
env["PYTHONUNBUFFERED"] = "1"

proc = subprocess.Popen(
    [sys.executable, str(SERVER)],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env=env,
    cwd=ROOT,
)


def send(obj):
    data = json.dumps(obj).encode("utf-8")
    proc.stdin.write(data + b"\n")   # <-- newline delimited, like the SDK
    proc.stdin.flush()


def read():
    line = proc.stdout.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


failures = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


# 1. initialize
send({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1.0"},
    },
})
r = read()
check("initialize returns a result", r and "result" in r, json.dumps(r)[:160] if r else "no response")
check("protocolVersion echoed", r and r["result"].get("protocolVersion") == "2025-06-18",
      str(r["result"].get("protocolVersion")) if r else "")
check("serverInfo present", r and r["result"].get("serverInfo", {}).get("name") == "opencode-telegram-bridge")
send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

# 2. tools/list
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
r = read()
tools = (r or {}).get("result", {}).get("tools", [])
names = sorted(t["name"] for t in tools)
check("tools/list returns 4 tools", len(tools) == 4, str(names))
for t in tools:
    check(f"tool {t['name']} has inputSchema", "inputSchema" in t)
    check(f"tool {t['name']} has description", bool(t.get("description")))

# 3. tools/call - session info (read only, safe)
send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
      "params": {"name": "telegram_session_info", "arguments": {}}})
r = read()
content = (r or {}).get("result", {}).get("content", [])
check("session_info returns content", bool(content))
check("session_info not an error", not (r or {}).get("result", {}).get("isError"))
text = content[0]["text"] if content else ""
check("session_info mentions model/provider", "model" in text.lower() or "session" in text.lower(),
      text[:200].replace("\n", " | "))
bot_token = load_config(require_token=True).bot_token
check("token never leaked", bot_token not in text)

# 4. tools/call - policy refusal for a non-allowlisted chat
send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
      "params": {"name": "telegram_send_message",
                 "arguments": {"text": "probe", "chat_id": "999999999"}}})
r = read()
res = (r or {}).get("result", {})
check("unlisted chat refused", res.get("isError") is True,
      (res.get("content") or [{}])[0].get("text", "")[:120])

# 5. tools/call - media path escape refused
send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
      "params": {"name": "telegram_send_document",
                 "arguments": {"file_path": "/etc/hostname"}}})
r = read()
res = (r or {}).get("result", {})
check("media escape refused", res.get("isError") is True,
      (res.get("content") or [{}])[0].get("text", "")[:120])

# 6. unknown method
send({"jsonrpc": "2.0", "id": 6, "method": "does/not/exist", "params": {}})
r = read()
check("unknown method -> JSON-RPC error", bool(r) and "error" in r, json.dumps(r)[:120])

proc.stdin.close()
try:
    proc.wait(timeout=5)
except Exception:
    proc.kill()

err = proc.stderr.read().decode("utf-8", "replace")
if err.strip():
    print("\n--- server stderr ---")
    print(err[:800])

print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
