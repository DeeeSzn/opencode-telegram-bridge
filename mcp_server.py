#!/usr/bin/env python3
"""Entry point for the OpenCode Telegram MCP server (stdio transport)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from oc_telegram.mcp_server import main

if __name__ == "__main__":
    raise SystemExit(main())
