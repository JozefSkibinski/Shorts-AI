"""Google Trends slash commands (``/youtube_trends``, ``/top_youtube_trends``,
``/compare_trends``, ``/trends_diff``).

This cog delegates all network work to :class:`TrendsService` and all
persistence to the repository classes. It is responsible for:

- Validating user input.
- Resolving per-guild defaults when a parameter was omitted.
- Offering keyword autocomplete via ``pytrends.suggestions``.
- Writing snapshots for historical diffs.
- Paginating large result sets with :class:`PaginatedEmbedView`.
- Translating service errors into friendly Discord embeds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.services.trends_service import (
    TrendsNoData, TrendsRateLimited, TrendsService,
    TrendsServiceError, TrendsUnavailable,
)
from bot.storage import (
    GuildSettings, GuildSettingsRepo, SnapshotRepo, TrendSnapshot,
)
from bot.storage.snapshot_repo import diff_top_queries
from bot.ui.pagination import PaginatedEmbedView
from bot.utils import charts, embeds, validators

log = logging.getLogger(__name__)


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

    def __init__(
        self,
        bot: commands.Bot,
        service: TrendsService,
        guild_repo: GuildSettingsRepo,
        snapshot_repo: SnapshotRepo,
    ) -> None:
        self.bot = bot
        self.service = service
        self.guild_repo = guild_repo
        self.snapshot_repo = snapshot_repo

    # -----------------------------------------------------------------
    # Autocomplete
    # -----------------------------------------------------------------
    async def _keyword_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest canonical keywords from Google Trends' suggestions API."""
        current = (current or "").strip()
        if len(current) < 2:
            return []
        try:
            suggestions = await self.service.suggestions(current)
        except Exception:
            log.debug("Suggestion lookup failed", exc_info=True)
            return []
        if not suggestions:
            # Always echo the user's current input so they can still submit it.
            return [app_commands.Choice(name=current[:100], value=current[:100])]
        out: list[app_commands.Choice[str]] = []
        for s in suggestions[:24]:
            label = s.title if not s.type else f"{s.title} — {s.type}"
            # Discord limits choice name/value to 100 chars.
            out.append(app_commands.Choice(name=label[:100], value=s.title[:100]))
        # Prepend the raw typed value so the user can override.
        out.insert(0, app_commands.Choice(name=f"Use “{current[:80]}”",
                                          value=current[:100]))
        return out[:25]

    # -----------------------------------------------------------------
    # /youtube_trends
    # -----------------------------------------------------------------
    @app_commands.command(
        name="youtube_trends",
        description="Show YouTube search trends related to a keyword.",
    )
    @app_commands.describe(
        keyword="Topic or phrase to look up on YouTube.",
        region="Region for the trends (default: guild default / Worldwide).",
        timeframe="How far back to look (default: guild default / 90 days).",
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

        defaults = await self._defaults(interaction)
        try:
            kw = validators.clean_keyword(keyword)
            geo = validators.validate_geo(
                region.value if region else defaults.default_geo
            )
            tf = validators.validate_timeframe(
                timeframe.value if timeframe else defaults.default_timeframe
            )
            lim = validators.clamp_limit(limit)
        except validators.ValidationError as exc:
            await interaction.followup.send(
                embed=embeds.error_embed(str(exc), title="Invalid input"),
                ephemeral=True,
            )
            return

        try:
            result = await self.service.related_queries(kw, geo=geo, timeframe=tf)
        except TrendsServiceError as exc:
            await _respond_with_service_error(interaction, exc)
            return

        # Persist a snapshot for later diffs. Failure here must never break
        # the user-facing response, so swallow repo errors.
        try:
            await self.snapshot_repo.insert(
                TrendSnapshot(
                    id=None,
                    captured_at=datetime.now(timezone.utc),
                    source="trends",
                    kind="related",
                    keyword=kw,
                    geo=geo,
                    timeframe=tf,
                    payload={"top": result.top, "rising": result.rising},
                )
            )
        except Exception:
            log.warning("Snapshot persist failed", exc_info=True)

        pages = embeds.paged_related_trends_embeds(
            keyword=kw, geo=geo, timeframe=tf,
            top=result.top, rising=result.rising, per_page=lim,
        )
        view = PaginatedEmbedView(pages, author_id=interaction.user.id)
        await view.send(interaction)

    # -----------------------------------------------------------------
    # /top_youtube_trends
    # -----------------------------------------------------------------
    @app_commands.command(
        name="top_youtube_trends",
        description="Show the top trending YouTube search queries for a region.",
    )
    @app_commands.describe(
        region="Region (default: guild default / Worldwide).",
        timeframe="How recently to look (default: past 7 days).",
        limit="How many trends to show (1-25, default: 10).",
        seed="Broad seed term; defaults to guild setting or 'youtube'.",
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

        defaults = await self._defaults(interaction)
        try:
            geo = validators.validate_geo(
                region.value if region else defaults.default_geo
            )
            tf = validators.validate_timeframe(
                timeframe.value if timeframe else "now 7-d"
            )
            lim = validators.clamp_limit(limit)
            seed_kw = validators.clean_keyword(seed) if seed else defaults.digest_seed
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

        try:
            await self.snapshot_repo.insert(
                TrendSnapshot(
                    id=None,
                    captured_at=datetime.now(timezone.utc),
                    source="trends",
                    kind="top",
                    keyword=seed_kw,
                    geo=geo,
                    timeframe=tf,
                    payload={"rows": rows},
                )
            )
        except Exception:
            log.warning("Snapshot persist failed", exc_info=True)

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
        region="Region (default: guild default / Worldwide).",
        timeframe="How far back to look (default: guild default / 90 days).",
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

        defaults = await self._defaults(interaction)
        try:
            kws = validators.parse_keyword_list(keywords)
            geo = validators.validate_geo(
                region.value if region else defaults.default_geo
            )
            tf = validators.validate_timeframe(
                timeframe.value if timeframe else defaults.default_timeframe
            )
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

        try:
            await self.snapshot_repo.insert(
                TrendSnapshot(
                    id=None,
                    captured_at=datetime.now(timezone.utc),
                    source="trends",
                    kind="compare",
                    keyword=",".join(sorted(k.lower() for k in kws)),
                    geo=geo,
                    timeframe=tf,
                    payload={"averages": averages, "winner": winner},
                )
            )
        except Exception:
            log.warning("Snapshot persist failed", exc_info=True)

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

        if file:
            await interaction.followup.send(embed=embed, file=file)
        else:
            await interaction.followup.send(embed=embed)

    # -----------------------------------------------------------------
    # /trends_diff — uses persisted snapshots
    # -----------------------------------------------------------------
    @app_commands.command(
        name="trends_diff",
        description="Week-over-week diff of related YouTube queries for a keyword.",
    )
    @app_commands.describe(
        keyword="The keyword to diff (must have been queried before).",
        region="Region (default: guild default / Worldwide).",
        timeframe="Timeframe of the original query (default: guild default).",
    )
    @app_commands.choices(region=_REGION_CHOICES, timeframe=_TIMEFRAME_CHOICES)
    async def trends_diff(
        self,
        interaction: discord.Interaction,
        keyword: str,
        region: Optional[app_commands.Choice[str]] = None,
        timeframe: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        defaults = await self._defaults(interaction)
        try:
            kw = validators.clean_keyword(keyword)
            geo = validators.validate_geo(
                region.value if region else defaults.default_geo
            )
            tf = validators.validate_timeframe(
                timeframe.value if timeframe else defaults.default_timeframe
            )
        except validators.ValidationError as exc:
            await interaction.followup.send(
                embed=embeds.error_embed(str(exc), title="Invalid input"),
                ephemeral=True,
            )
            return

        # Fetch a fresh result so the "current" side is always up to date.
        try:
            current = await self.service.related_queries(kw, geo=geo, timeframe=tf)
        except TrendsServiceError as exc:
            await _respond_with_service_error(interaction, exc)
            return

        previous = await self.snapshot_repo.week_ago(
            keyword=kw, geo=geo, timeframe=tf, kind="related"
        )
        if previous is None:
            await interaction.followup.send(
                embed=embeds.info_embed(
                    (
                        "No snapshot from ≥6 days ago is stored for "
                        f"`{kw}` / `{geo or 'WW'}` / `{tf}`. "
                        "Run `/youtube_trends` now and try again in a week."
                    ),
                    title="Nothing to diff yet",
                ),
                ephemeral=True,
            )
            return

        prev_rows = previous.payload.get("top", []) or []
        cur_rows = current.top or []
        diff = diff_top_queries(cur_rows, prev_rows)

        embed = embeds.diff_embed(
            keyword=kw,
            geo=geo,
            timeframe=tf,
            diff=diff,
            previous_timestamp=previous.captured_at.strftime("%Y-%m-%d %H:%M UTC"),
        )
        await interaction.followup.send(embed=embed)

    # -----------------------------------------------------------------
    # helpers
    # -----------------------------------------------------------------
    async def _defaults(self, interaction: discord.Interaction) -> GuildSettings:
        if interaction.guild_id is None:
            return GuildSettings(guild_id=0)
        try:
            return await self.guild_repo.get_or_default(interaction.guild_id)
        except Exception:
            log.warning("Guild-settings fetch failed; using defaults", exc_info=True)
            return GuildSettings(guild_id=interaction.guild_id)


# Register autocomplete for every keyword-accepting command after the class
# is defined (decorator form needs a bound method reference).
def _install_autocompletes(cog: TrendsCog) -> None:
    cog.youtube_trends.autocomplete("keyword")(cog._keyword_autocomplete)
    cog.trends_diff.autocomplete("keyword")(cog._keyword_autocomplete)
    cog.top_youtube_trends.autocomplete("seed")(cog._keyword_autocomplete)


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
    import asyncio
    title = f"YouTube interest over time — {geo or 'Worldwide'} • {tf}"
    return await asyncio.to_thread(
        charts.render_interest_over_time, frame, kws, title=title
    )


def _wrap_bytes(data: bytes):
    import io
    return io.BytesIO(data)


async def setup_cog(
    bot: commands.Bot,
    service: TrendsService,
    guild_repo: GuildSettingsRepo,
    snapshot_repo: SnapshotRepo,
) -> TrendsCog:
    cog = TrendsCog(bot, service, guild_repo, snapshot_repo)
    _install_autocompletes(cog)
    await bot.add_cog(cog)
    return cog
