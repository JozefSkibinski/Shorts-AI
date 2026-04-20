"""Bot factory and lifecycle management."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from bot.cogs.trends import setup_cog as setup_trends_cog
from bot.config import Settings
from bot.services.trends_service import TrendsService

log = logging.getLogger(__name__)


class ShortsAIBot(commands.Bot):
    """Thin ``commands.Bot`` subclass that wires our cogs + services together."""

    def __init__(self, settings: Settings) -> None:
        # We only need basic gateway intents; slash commands don't require
        # ``message_content`` or ``members``.
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.settings = settings
        self.trends_service = TrendsService(
            timeout_connect=settings.pytrends_timeout_connect,
            timeout_read=settings.pytrends_timeout_read,
            proxy=settings.pytrends_proxy,
        )

    async def setup_hook(self) -> None:
        """Called once before the bot connects; load cogs and sync commands."""
        await setup_trends_cog(self, self.trends_service)

        # Route slash-command errors into our handler.
        self.tree.on_error = self._on_tree_error  # type: ignore[assignment]

        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            # Copying global commands into a guild then syncing makes them
            # appear immediately in that guild (useful while developing).
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info(
                "Synced %d slash commands to guild %s",
                len(synced),
                self.settings.guild_id,
            )
        else:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands globally", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id if self.user else "?")

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
