"""Shared logging setup.

We keep a single root configuration so every module can just do
``logger = logging.getLogger(__name__)`` and inherit the format.
"""

from __future__ import annotations

import logging
import sys


_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger once.

    Calling this more than once is safe; we only attach our handler the first
    time so test runners and reloads don't produce duplicate log lines.
    """
    root = logging.getLogger()
    root.setLevel(level)

    already_configured = any(
        getattr(h, "_shorts_ai_handler", False) for h in root.handlers
    )
    if already_configured:
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler._shorts_ai_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    # discord.py is chatty at DEBUG; keep it one notch below the app level.
    logging.getLogger("discord").setLevel(
        max(logging.INFO, logging.getLogger().level)
    )
