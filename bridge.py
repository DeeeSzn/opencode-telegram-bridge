#!/usr/bin/env python3
"""Entry point for the inbound Telegram -> OpenCode bridge daemon."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from oc_telegram.bridge import main

if __name__ == "__main__":
    raise SystemExit(main())
