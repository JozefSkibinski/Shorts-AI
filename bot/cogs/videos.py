"""YouTube Data API v3 slash commands (``/youtube_videos``).

Separate from :mod:`bot.cogs.trends` because the underlying data source is
different — actual YouTube videos with view counts, not Google Trends
relative search interest.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.services.youtube_data_service import (
    YouTubeAPIError, YouTubeAPINotConfigured, YouTubeDataService,
    YouTubeQuotaExceeded, YouTubeAPIUnavailable,
)
from bot.storage import GuildSettingsRepo
from bot.utils import embeds, validators

log = logging.getLogger(__name__)


class VideosCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        yt_service: YouTubeDataService,
        guild_repo: GuildSettingsRepo,
    ) -> None:
        self.bot = bot
        self.yt = yt_service
        self.guild_repo = guild_repo

    @app_commands.command(
        name="youtube_videos",
        description="Top YouTube videos for a query (YouTube Data API, not Trends).",
    )
    @app_commands.describe(
        query="Search query.",
        region="ISO alpha-2 region code (default: guild default / Worldwide).",
        limit="How many videos to return (1-15, default: 5).",
    )
    async def youtube_videos(
        self,
        interaction: discord.Interaction,
        query: str,
        region: Optional[str] = None,
        limit: Optional[app_commands.Range[int, 1, 15]] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        default_geo = ""
        if interaction.guild_id is not None:
            try:
                gs = await self.guild_repo.get_or_default(interaction.guild_id)
                default_geo = gs.default_geo
            except Exception:
                log.debug("Guild settings fetch failed", exc_info=True)

        try:
            q = validators.clean_keyword(query)
            geo = validators.validate_geo(region if region is not None else default_geo)
            lim = validators.clamp_limit(limit, default=5, lo=1, hi=15)
        except validators.ValidationError as exc:
            await interaction.followup.send(
                embed=embeds.error_embed(str(exc), title="Invalid input"),
                ephemeral=True,
            )
            return

        try:
            videos = await self.yt.top_videos(
                query=q, region_code=geo, max_results=lim
            )
        except YouTubeAPINotConfigured as exc:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    str(exc), title="YouTube Data API not configured"
                ),
                ephemeral=True,
            )
            return
        except YouTubeQuotaExceeded as exc:
            await interaction.followup.send(
                embed=embeds.error_embed(str(exc), title="Daily quota exceeded"),
                ephemeral=True,
            )
            return
        except YouTubeAPIUnavailable as exc:
            await interaction.followup.send(
                embed=embeds.error_embed(str(exc), title="YouTube API error"),
                ephemeral=True,
            )
            return
        except YouTubeAPIError as exc:
            log.exception("Unhandled YouTube API error")
            await interaction.followup.send(
                embed=embeds.error_embed(str(exc), title="YouTube API error"),
                ephemeral=True,
            )
            return

        embed = embeds.youtube_videos_embed(
            query=q, region=geo, videos=[asdict(v) | {"url": v.url} for v in videos]
        )
        await interaction.followup.send(embed=embed)


async def setup_cog(
    bot: commands.Bot,
    yt_service: YouTubeDataService,
    guild_repo: GuildSettingsRepo,
) -> None:
    await bot.add_cog(VideosCog(bot, yt_service, guild_repo))
