"""Daily trend-digest scheduler (``discord.ext.tasks``).

Runs once per day at ``DAILY_DIGEST_HOUR_UTC:MM``. For every guild that has
opted in (via ``/trends_settings set digest_channel:#…``) it posts the top
YouTube trend queries plus — if a prior snapshot exists — a week-over-week
diff.

The scheduler is resilient:

- It waits for :meth:`Bot.wait_until_ready` before the first tick.
- A failed guild doesn't abort the whole run; each guild is wrapped in
  try/except and logged independently.
- If the bot disconnects while the task is sleeping, ``tasks.loop`` will
  reschedule after reconnect without us doing anything.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

from bot.config import Settings
from bot.services.trends_service import (
    TrendsNoData, TrendsService, TrendsServiceError,
)
from bot.storage import GuildSettingsRepo, SnapshotRepo, TrendSnapshot
from bot.storage.snapshot_repo import diff_top_queries
from bot.utils import embeds

log = logging.getLogger(__name__)


class SchedulerCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        settings: Settings,
        trends_service: TrendsService,
        guild_repo: GuildSettingsRepo,
        snapshot_repo: SnapshotRepo,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.trends = trends_service
        self.guild_repo = guild_repo
        self.snapshot_repo = snapshot_repo

        # ``time=`` with a timezone makes ``tasks.loop`` fire once when the
        # local wall clock hits that time — exactly once per day.
        run_at = time(
            hour=settings.daily_digest_hour_utc,
            minute=settings.daily_digest_minute_utc,
            tzinfo=timezone.utc,
        )
        self.daily_digest.change_interval(time=run_at)

    async def cog_load(self) -> None:
        if self.settings.daily_digest_enabled:
            self.daily_digest.start()
            log.info(
                "Daily digest scheduled for %02d:%02d UTC",
                self.settings.daily_digest_hour_utc,
                self.settings.daily_digest_minute_utc,
            )
        else:
            log.info("Daily digest disabled via config; scheduler idle.")

    async def cog_unload(self) -> None:
        if self.daily_digest.is_running():
            self.daily_digest.cancel()

    # -- the scheduled task -------------------------------------------------
    # Interval is overridden in __init__ via change_interval(time=...). The
    # decorator values are just placeholders that ensure the task exists.
    @tasks.loop(hours=24)
    async def daily_digest(self) -> None:
        try:
            guilds = await self.guild_repo.guilds_with_digest()
        except Exception:
            log.exception("Failed to load guilds_with_digest; skipping this tick.")
            return

        if not guilds:
            log.info("Daily digest tick: no guilds opted in.")
            return

        log.info("Daily digest tick: %d guild(s) opted in.", len(guilds))
        for gs in guilds:
            try:
                await self._post_for_guild(gs)
            except Exception:
                log.exception("Digest failed for guild %s", gs.guild_id)
            # Space out the Google Trends requests to reduce rate-limit pressure.
            await asyncio.sleep(3)

    @daily_digest.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    @daily_digest.error
    async def _on_error(self, error: BaseException) -> None:
        log.exception("daily_digest task raised", exc_info=error)

    # -- per-guild posting --------------------------------------------------

    async def _post_for_guild(self, gs) -> None:
        assert gs.digest_channel_id is not None
        channel = self.bot.get_channel(gs.digest_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(gs.digest_channel_id)
            except (discord.NotFound, discord.Forbidden):
                log.warning(
                    "Digest channel %s not accessible for guild %s; skipping.",
                    gs.digest_channel_id, gs.guild_id,
                )
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.warning(
                "Digest channel %s is not a text channel; skipping.",
                gs.digest_channel_id,
            )
            return

        geo = gs.default_geo
        timeframe = "now 7-d"
        seed = gs.digest_seed or "youtube"
        try:
            rows = await self.trends.top_trends(
                seed=seed, geo=geo, timeframe=timeframe, limit=10
            )
        except TrendsNoData:
            log.info("No digest data for guild %s (%s/%s)", gs.guild_id, seed, geo)
            return
        except TrendsServiceError as exc:
            log.warning("Digest fetch failed for guild %s: %s", gs.guild_id, exc)
            return

        # Persist + compute diff.
        try:
            await self.snapshot_repo.insert(
                TrendSnapshot(
                    id=None,
                    captured_at=datetime.now(timezone.utc),
                    source="trends",
                    kind="top",
                    keyword=seed,
                    geo=geo,
                    timeframe=timeframe,
                    payload={"rows": rows},
                )
            )
        except Exception:
            log.warning("Snapshot persist failed in digest", exc_info=True)

        previous = await self.snapshot_repo.week_ago(
            keyword=seed, geo=geo, timeframe=timeframe, kind="top"
        )

        header = embeds.top_trends_embed(
            geo=geo, timeframe=timeframe, rows=rows, seed=seed
        )
        header.title = f"📅 Daily digest — {header.title}"

        try:
            await channel.send(embed=header)
        except discord.Forbidden:
            log.warning(
                "No permission to post digest in channel %s (guild %s)",
                gs.digest_channel_id, gs.guild_id,
            )
            return

        if previous is not None:
            diff = diff_top_queries(rows, previous.payload.get("rows", []) or [])
            diff_body = embeds.diff_embed(
                keyword=seed,
                geo=geo,
                timeframe=timeframe,
                diff=diff,
                previous_timestamp=previous.captured_at.strftime(
                    "%Y-%m-%d %H:%M UTC"
                ),
            )
            try:
                await channel.send(embed=diff_body)
            except discord.HTTPException:
                log.debug("Failed to post diff", exc_info=True)


async def setup_cog(
    bot: commands.Bot,
    settings: Settings,
    trends_service: TrendsService,
    guild_repo: GuildSettingsRepo,
    snapshot_repo: SnapshotRepo,
) -> None:
    await bot.add_cog(
        SchedulerCog(bot, settings, trends_service, guild_repo, snapshot_repo)
    )
