"""Bot factory and lifecycle management."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from bot.cogs.scheduler import setup_cog as setup_scheduler_cog
from bot.cogs.settings import setup_cog as setup_settings_cog
from bot.cogs.trends import setup_cog as setup_trends_cog
from bot.cogs.videos import setup_cog as setup_videos_cog
from bot.config import Settings
from bot.healthcheck import HealthcheckServer
from bot.services.cache import TTLKeyCache
from bot.services.trends_service import TrendsService
from bot.services.youtube_data_service import YouTubeDataService
from bot.storage import (
    GuildSettingsRepo, SnapshotRepo, build_database,
)

log = logging.getLogger(__name__)


class ShortsAIBot(commands.Bot):
    """Thin ``commands.Bot`` subclass that wires our cogs + services together."""

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.settings = settings

        # -- caches (shared between services) -----------------------
        self.result_cache = TTLKeyCache(
            maxsize=settings.cache_max_size,
            ttl=settings.cache_ttl_seconds,
        )
        self.suggestion_cache = TTLKeyCache(
            maxsize=max(64, settings.cache_max_size // 4),
            ttl=settings.suggestion_cache_ttl_seconds,
        )

        # -- services -----------------------------------------------
        self.trends_service = TrendsService(
            timeout_connect=settings.pytrends_timeout_connect,
            timeout_read=settings.pytrends_timeout_read,
            proxy=settings.pytrends_proxy,
            result_cache=self.result_cache,
            suggestion_cache=self.suggestion_cache,
        )
        self.youtube_service = YouTubeDataService(
            api_key=settings.youtube_api_key,
            cache=self.result_cache,
        )

        # -- storage (DB + repositories) ----------------------------
        self.database = build_database(settings.database_url)
        self.guild_repo = GuildSettingsRepo(self.database)
        self.snapshot_repo = SnapshotRepo(self.database)

        # -- healthcheck server (started later) ---------------------
        self.healthcheck = (
            HealthcheckServer(self, host=settings.healthcheck_host,
                              port=settings.healthcheck_port)
            if settings.healthcheck_enabled else None
        )

    async def setup_hook(self) -> None:
        """Called once before the bot connects; load cogs and sync commands."""
        # 1. Bring up the database before anything that uses it.
        await self.database.connect()
        await self.database.init_schema()

        # 2. Cogs.
        await setup_trends_cog(
            self, self.trends_service, self.guild_repo, self.snapshot_repo
        )
        await setup_settings_cog(self, self.guild_repo)
        await setup_videos_cog(self, self.youtube_service, self.guild_repo)
        await setup_scheduler_cog(
            self, self.settings, self.trends_service,
            self.guild_repo, self.snapshot_repo,
        )

        # 3. Error routing.
        self.tree.on_error = self._on_tree_error  # type: ignore[assignment]

        # 4. Slash-command sync.
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info(
                "Synced %d slash commands to guild %s",
                len(synced), self.settings.guild_id,
            )
        else:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands globally", len(synced))

        # 5. Healthcheck.
        if self.healthcheck is not None:
            try:
                await self.healthcheck.start()
            except OSError:
                log.exception(
                    "Healthcheck failed to bind %s:%s; continuing without it.",
                    self.settings.healthcheck_host, self.settings.healthcheck_port,
                )
                self.healthcheck = None

    async def close(self) -> None:
        """Graceful shutdown — stop healthcheck, close DB + HTTP sessions."""
        try:
            if self.healthcheck is not None:
                await self.healthcheck.stop()
        except Exception:
            log.exception("Error stopping healthcheck")
        try:
            await self.youtube_service.close()
        except Exception:
            log.exception("Error closing YouTube HTTP session")
        try:
            await self.database.close()
        except Exception:
            log.exception("Error closing database")
        await super().close()

    async def on_ready(self) -> None:
        log.info(
            "Logged in as %s (id=%s)",
            self.user, self.user.id if self.user else "?",
        )

    async def _on_tree_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        """Catch-all for errors raised from slash commands."""
        log.exception("Unhandled app-command error", exc_info=error)

        message = "Something unexpected went wrong. Please try again later."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.exception("Failed to deliver generic error message to user")


def build_bot(settings: Settings) -> ShortsAIBot:
    return ShortsAIBot(settings)
