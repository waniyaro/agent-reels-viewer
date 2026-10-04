#!/usr/bin/env python3
"""Compatibility wrapper for Antigravity skill invocation."""

import os
import sys

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent_reels_viewer.cli import main

if __name__ == "__main__":
    sys.exit(main())
