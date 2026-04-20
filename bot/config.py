"""Centralised configuration loaded from environment variables.

The module is intentionally tiny: it reads from the environment once at import
time via python-dotenv and exposes a frozen dataclass. This keeps the rest of
the bot free of ``os.environ`` lookups.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

# Load variables from a local `.env` file if one exists. In production you can
# also inject the variables via systemd / docker / your platform of choice.
load_dotenv()


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings."""

    discord_token: str
    guild_id: Optional[int]
    log_level: str
    pytrends_timeout_connect: int
    pytrends_timeout_read: int
    pytrends_proxy: Optional[str]


def load_settings() -> Settings:
    """Build a :class:`Settings` instance from the current environment."""
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is missing. Copy .env.example to .env and set it."
        )

    raw_guild = os.getenv("DISCORD_GUILD_ID", "").strip()
    guild_id = int(raw_guild) if raw_guild.isdigit() else None

    proxy = os.getenv("PYTRENDS_PROXY", "").strip() or None

    return Settings(
        discord_token=token,
        guild_id=guild_id,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        pytrends_timeout_connect=_get_int("PYTRENDS_TIMEOUT_CONNECT", 10),
        pytrends_timeout_read=_get_int("PYTRENDS_TIMEOUT_READ", 25),
        pytrends_proxy=proxy,
    )
