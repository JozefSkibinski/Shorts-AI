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


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings."""

    # Discord
    discord_token: str
    guild_id: Optional[int]
    log_level: str

    # pytrends HTTP tuning
    pytrends_timeout_connect: int
    pytrends_timeout_read: int
    pytrends_proxy: Optional[str]

    # Cache
    cache_ttl_seconds: int
    cache_max_size: int
    suggestion_cache_ttl_seconds: int

    # Storage
    database_url: str                       # e.g. sqlite:///data/shorts_ai.db

    # Scheduler
    daily_digest_enabled: bool
    daily_digest_hour_utc: int              # 0-23
    daily_digest_minute_utc: int            # 0-59

    # Healthcheck
    healthcheck_enabled: bool
    healthcheck_host: str
    healthcheck_port: int

    # YouTube Data API (optional)
    youtube_api_key: Optional[str]


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
        cache_ttl_seconds=_get_int("CACHE_TTL_SECONDS", 600),
        cache_max_size=_get_int("CACHE_MAX_SIZE", 512),
        suggestion_cache_ttl_seconds=_get_int("SUGGESTION_CACHE_TTL_SECONDS", 3600),
        database_url=os.getenv(
            "DATABASE_URL", "sqlite:///data/shorts_ai.db"
        ).strip(),
        daily_digest_enabled=_get_bool("DAILY_DIGEST_ENABLED", False),
        daily_digest_hour_utc=max(0, min(23, _get_int("DAILY_DIGEST_HOUR_UTC", 13))),
        daily_digest_minute_utc=max(0, min(59, _get_int("DAILY_DIGEST_MINUTE_UTC", 0))),
        healthcheck_enabled=_get_bool("HEALTHCHECK_ENABLED", True),
        healthcheck_host=os.getenv("HEALTHCHECK_HOST", "0.0.0.0").strip(),
        healthcheck_port=_get_int("HEALTHCHECK_PORT", 8080),
        youtube_api_key=os.getenv("YOUTUBE_API_KEY", "").strip() or None,
    )
