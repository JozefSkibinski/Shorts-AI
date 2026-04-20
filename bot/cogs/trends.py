"""The ``/youtube_trends``, ``/top_youtube_trends`` and ``/compare_trends`` cog."""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.services.trends_service import (
    TrendsNoData,
    TrendsRateLimited,
    TrendsService,
    TrendsServiceError,
    TrendsUnavailable,
)
from bot.utils import charts, embeds, validators

log = logging.getLogger(__name__)


# Curated list of timeframes exposed as a dropdown. pytrends accepts more
# values but these cover the common cases and sidestep user typos.
_TIMEFRAME_CHOICES = [
    app_commands.Choice(name="Past hour",    value="now 1-H"),
    app_commands.Choice(name="Past 4 hours", value="now 4-H"),
    app_commands.Choice(name="Past day",     value="now 1-d"),
    app_commands.Choice(name="Past 7 days",  value="now 7-d"),
    app_commands.Choice(name="Past 30 days", value="today 1-m"),
    app_commands.Choice(name="Past 90 days", value="today 3-m"),
    app_commands.Choice(name="Past 12 months", value="today 12-m"),
    app_commands.Choice(name="Past 5 years", value="today 5-y"),
]

# A handful of popular regions for the autocomplete dropdown; any ISO-3166
# alpha-2 code still works via manual entry because `validate_geo` accepts
# them. Discord caps choices at 25 so we keep this list reasonably small.
_REGION_CHOICES = [
    app_commands.Choice(name="Worldwide", value=""),
    app_commands.Choice(name="United States", value="US"),
    app_commands.Choice(name="United Kingdom", value="GB"),
    app_commands.Choice(name="India", value="IN"),
    app_commands.Choice(name="Canada", value="CA"),
    app_commands.Choice(name="Australia", value="AU"),
    app_commands.Choice(name="Germany", value="DE"),
    app_commands.Choice(name="France", value="FR"),
    app_commands.Choice(name="Brazil", value="BR"),
    app_commands.Choice(name="Japan", value="JP"),
    app_commands.Choice(name="South Korea", value="KR"),
    app_commands.Choice(name="Mexico", value="MX"),
    app_commands.Choice(name="Spain", value="ES"),
    app_commands.Choice(name="Italy", value="IT"),
    app_commands.Choice(name="Netherlands", value="NL"),
    app_commands.Choice(name="Indonesia", value="ID"),
    app_commands.Choice(name="Nigeria", value="NG"),
    app_commands.Choice(name="South Africa", value="ZA"),
    app_commands.Choice(name="Argentina", value="AR"),
    app_commands.Choice(name="Philippines", value="PH"),
]


class TrendsCog(commands.Cog):
    """Slash commands that expose Google Trends' YouTube data."""

    def __init__(self, bot: commands.Bot, service: TrendsService) -> None:
        self.bot = bot
        self.service = service

    # -----------------------------------------------------------------
    # /youtube_trends
    # -----------------------------------------------------------------
    @app_commands.command(
        name="youtube_trends",
        description="Show YouTube search trends related to a keyword.",
    )
    @app_commands.describe(
        keyword="Topic or phrase to look up on YouTube (e.g. 'lofi beats').",
        region="Region for the trends (default: Worldwide).",
        timeframe="How far back to look (default: past 90 days).",
        limit="Max entries per list (1-25, default: 10).",
    )
    @app_commands.choices(region=_REGION_CHOICES, timeframe=_TIMEFRAME_CHOICES)
    async def youtube_trends(
        self,
        interaction: discord.Interaction,
        keyword: str,
        region: Optional[app_commands.Choice[str]] = None,
        timeframe: Optional[app_commands.Choice[str]] = None,
        limit: Optional[app_commands.Range[int, 1, 25]] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            kw = validators.clean_keyword(keyword)
            geo = validators.validate_geo(region.value if region else None)
            tf = validators.validate_timeframe(timeframe.value if timeframe else None)
            lim = validators.clamp_limit(limit)
        except validators.ValidationError as exc:
            await interaction.followup.send(
                embed=embeds.error_embed(str(exc), title="Invalid input"),
                ephemeral=True,
            )
            return

        try:
            result = await self.service.related_queries(
                kw, geo=geo, timeframe=tf
            )
        except TrendsServiceError as exc:
            await _respond_with_service_error(interaction, exc)
            return

        embed = embeds.related_trends_embed(
            keyword=kw,
            geo=geo,
            timeframe=tf,
            top=result.top,
            rising=result.rising,
            limit=lim,
        )
        await interaction.followup.send(embed=embed)

    # -----------------------------------------------------------------
    # /top_youtube_trends
    # -----------------------------------------------------------------
    @app_commands.command(
        name="top_youtube_trends",
        description="Show the top trending YouTube search queries for a region.",
    )
    @app_commands.describe(
        region="Region (default: Worldwide).",
        timeframe="How recently to look (default: past 7 days).",
        limit="How many trends to show (1-25, default: 10).",
        seed=(
            "Broad seed term used to discover trends. "
            "Defaults to 'youtube'; try 'music', 'gaming', etc."
        ),
    )
    @app_commands.choices(region=_REGION_CHOICES, timeframe=_TIMEFRAME_CHOICES)
    async def top_youtube_trends(
        self,
        interaction: discord.Interaction,
        region: Optional[app_commands.Choice[str]] = None,
        timeframe: Optional[app_commands.Choice[str]] = None,
        limit: Optional[app_commands.Range[int, 1, 25]] = None,
        seed: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            geo = validators.validate_geo(region.value if region else None)
            tf = validators.validate_timeframe(
                timeframe.value if timeframe else "now 7-d"
            )
            lim = validators.clamp_limit(limit)
            seed_kw = validators.clean_keyword(seed) if seed else "youtube"
        except validators.ValidationError as exc:
            await interaction.followup.send(
                embed=embeds.error_embed(str(exc), title="Invalid input"),
                ephemeral=True,
            )
            return

        try:
            rows = await self.service.top_trends(
                seed=seed_kw, geo=geo, timeframe=tf, limit=lim
            )
        except TrendsServiceError as exc:
            await _respond_with_service_error(interaction, exc)
            return

        embed = embeds.top_trends_embed(
            geo=geo, timeframe=tf, rows=rows, seed=seed_kw
        )
        await interaction.followup.send(embed=embed)

    # -----------------------------------------------------------------
    # /compare_trends
    # -----------------------------------------------------------------
    @app_commands.command(
        name="compare_trends",
        description="Compare YouTube interest for 2-5 keywords (comma separated).",
    )
    @app_commands.describe(
        keywords="2-5 keywords separated by commas. Example: 'python, rust, go'.",
        region="Region (default: Worldwide).",
        timeframe="How far back to look (default: past 90 days).",
        chart="Attach a line chart of interest over time (default: True).",
    )
    @app_commands.choices(region=_REGION_CHOICES, timeframe=_TIMEFRAME_CHOICES)
    async def compare_trends(
        self,
        interaction: discord.Interaction,
        keywords: str,
        region: Optional[app_commands.Choice[str]] = None,
        timeframe: Optional[app_commands.Choice[str]] = None,
        chart: Optional[bool] = True,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            kws = validators.parse_keyword_list(keywords)
            geo = validators.validate_geo(region.value if region else None)
            tf = validators.validate_timeframe(timeframe.value if timeframe else None)
        except validators.ValidationError as exc:
            await interaction.followup.send(
                embed=embeds.error_embed(str(exc), title="Invalid input"),
                ephemeral=True,
            )
            return

        try:
            frame, averages, winner = await self.service.compare_keywords(
                kws, geo=geo, timeframe=tf
            )
        except TrendsServiceError as exc:
            await _respond_with_service_error(interaction, exc)
            return

        embed = embeds.compare_trends_embed(
            keywords=kws, averages=averages, winner=winner, geo=geo, timeframe=tf
        )

        file: Optional[discord.File] = None
        if chart:
            try:
                png_bytes = await _render_chart_off_thread(frame, kws, geo, tf)
                file = discord.File(
                    fp=_wrap_bytes(png_bytes), filename="compare_trends.png"
                )
                embed.set_image(url="attachment://compare_trends.png")
            except Exception:
                log.exception("Chart rendering failed; sending text-only response.")
                # Non-fatal: the embed still has the textual summary.

        await interaction.followup.send(embed=embed, file=file) if file else \
            await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


async def _respond_with_service_error(
    interaction: discord.Interaction, exc: TrendsServiceError
) -> None:
    """Translate service exceptions into user-facing embeds."""
    if isinstance(exc, TrendsRateLimited):
        title, body = "Rate limited", str(exc)
    elif isinstance(exc, TrendsNoData):
        title, body = "No data", str(exc)
    elif isinstance(exc, TrendsUnavailable):
        title, body = "Google Trends unavailable", str(exc)
    else:
        title, body = "Something went wrong", str(exc) or "Unknown error."
    try:
        await interaction.followup.send(
            embed=embeds.error_embed(body, title=title), ephemeral=True
        )
    except discord.HTTPException:
        log.exception("Failed to deliver error embed to user")


async def _render_chart_off_thread(frame, kws, geo, tf) -> bytes:
    """Run matplotlib rendering in a worker thread to keep the loop free."""
    import asyncio

    title = f"YouTube interest over time — {geo or 'Worldwide'} • {tf}"
    return await asyncio.to_thread(
        charts.render_interest_over_time, frame, kws, title=title
    )


def _wrap_bytes(data: bytes):
    import io

    return io.BytesIO(data)


async def setup_cog(bot: commands.Bot, service: TrendsService) -> None:
    """Attach the cog to the bot. Called from ``bot.bot``."""
    await bot.add_cog(TrendsCog(bot, service))
