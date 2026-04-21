"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

# Add the project root to sys.path so `import bot...` works when running
# `pytest` from either the repo root or the tests/ folder.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
