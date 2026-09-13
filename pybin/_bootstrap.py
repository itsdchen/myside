"""Utility helpers to set up sys.path/working dir for pybin scripts."""
from __future__ import annotations
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[0]

# Ensure repo root is in sys.path for relative imports
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Some scripts assume cwd == repo root
if Path.cwd() != REPO_ROOT:
    os.chdir(REPO_ROOT)
