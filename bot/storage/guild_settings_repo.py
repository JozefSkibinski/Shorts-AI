"""Repository for per-guild configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bot.storage.database import Database


@dataclass(frozen=True)
class GuildSettings:
    """Per-guild defaults that override the bot-wide defaults."""

    guild_id: int
    default_geo: str = ""
    default_timeframe: str = "today 3-m"
    digest_channel_id: Optional[int] = None
    digest_seed: str = "youtube"


class GuildSettingsRepo:
    """CRUD helpers for :class:`GuildSettings`."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, guild_id: int) -> Optional[GuildSettings]:
        row = await self._db.fetchone(
            "SELECT guild_id, default_geo, default_timeframe, "
            "       digest_channel_id, digest_seed "
            "FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        if row is None:
            return None
        return GuildSettings(
            guild_id=row["guild_id"],
            default_geo=row["default_geo"] or "",
            default_timeframe=row["default_timeframe"] or "today 3-m",
            digest_channel_id=row["digest_channel_id"],
            digest_seed=row["digest_seed"] or "youtube",
        )

    async def get_or_default(self, guild_id: int) -> GuildSettings:
        """Return stored settings or a sensible default instance."""
        existing = await self.get(guild_id)
        return existing or GuildSettings(guild_id=guild_id)

    async def upsert(self, settings: GuildSettings) -> None:
        await self._db.execute(
            """
            INSERT INTO guild_settings (
                guild_id, default_geo, default_timeframe,
                digest_channel_id, digest_seed, updated_at
            )
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(guild_id) DO UPDATE SET
                default_geo       = excluded.default_geo,
                default_timeframe = excluded.default_timeframe,
                digest_channel_id = excluded.digest_channel_id,
                digest_seed       = excluded.digest_seed,
                updated_at        = datetime('now')
            """,
            (
                settings.guild_id,
                settings.default_geo,
                settings.default_timeframe,
                settings.digest_channel_id,
                settings.digest_seed,
            ),
        )

    async def guilds_with_digest(self) -> list[GuildSettings]:
        """Return every guild that has opted into the daily digest."""
        rows = await self._db.fetchall(
            "SELECT guild_id, default_geo, default_timeframe, "
            "       digest_channel_id, digest_seed "
            "FROM guild_settings "
            "WHERE digest_channel_id IS NOT NULL"
        )
        return [
            GuildSettings(
                guild_id=r["guild_id"],
                default_geo=r["default_geo"] or "",
                default_timeframe=r["default_timeframe"] or "today 3-m",
                digest_channel_id=r["digest_channel_id"],
                digest_seed=r["digest_seed"] or "youtube",
            )
            for r in rows
        ]
