#!/usr/bin/env python3
"""Codex hook adapter; keep stdout JSON and never block the host on failures."""

import os
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from scripts.contextctl import main_hook  # noqa: E402


if __name__ == "__main__":
    main_hook()
