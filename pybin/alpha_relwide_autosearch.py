#!/usr/bin/env python3
"""Convenience wrapper for alpha_relwide_autosearch CLI."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if __name__ == "__main__":
    runpy.run_module("overmind.strat_main.tools.trademan.alpha_relwide_autosearch", run_name="__main__")
