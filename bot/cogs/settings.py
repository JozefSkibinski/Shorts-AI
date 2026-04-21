"""Per-guild configuration commands.

These commands let server admins set defaults that other trend commands will
use when parameters are omitted, plus opt the guild into the daily digest.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.storage import GuildSettings, GuildSettingsRepo
from bot.utils import embeds, validators

log = logging.getLogger(__name__)


class SettingsCog(commands.Cog):
    settings_group = app_commands.Group(
        name="trends_settings",
        description="Configure per-guild defaults for the trend commands.",
        # Require "Manage Server" by default; admins can override per command.
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot, repo: GuildSettingsRepo) -> None:
        self.bot = bot
        self.repo = repo

    # ------ /trends_settings show ------------------------------------
    @settings_group.command(name="show", description="Show this server's current defaults.")
    async def show(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        current = await self.repo.get_or_default(interaction.guild_id)

        digest_channel = (
            f"<#{current.digest_channel_id}>" if current.digest_channel_id else "_not set_"
        )
        embed = discord.Embed(
            title="Trend bot settings",
            color=0x5865F2,
            description=(
                f"**Default region:** `{current.default_geo or 'Worldwide'}`\n"
                f"**Default timeframe:** `{current.default_timeframe}`\n"
                f"**Digest seed keyword:** `{current.digest_seed}`\n"
                f"**Daily digest channel:** {digest_channel}"
            ),
        )
        embed.set_footer(text="Use /trends_settings set to change any of these.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------ /trends_settings set -------------------------------------
    @settings_group.command(
        name="set",
        description="Update one or more defaults for this server.",
    )
    @app_commands.describe(
        region="ISO-3166 alpha-2 code (e.g. US, GB, IN) or empty for Worldwide.",
        timeframe="Google Trends timeframe (e.g. 'today 3-m', 'now 7-d').",
        digest_channel="Channel where daily trend digests should be posted.",
        digest_seed="Broad seed used for the daily digest (default 'youtube').",
    )
    async def set_(
        self,
        interaction: discord.Interaction,
        region: Optional[str] = None,
        timeframe: Optional[str] = None,
        digest_channel: Optional[discord.TextChannel] = None,
        digest_seed: Optional[str] = None,
    ) -> None:
        assert interaction.guild_id is not None
        current = await self.repo.get_or_default(interaction.guild_id)
        try:
            new_geo = (
                validators.validate_geo(region)
                if region is not None
                else current.default_geo
            )
            new_tf = (
                validators.validate_timeframe(timeframe)
                if timeframe is not None
                else current.default_timeframe
            )
            new_seed = (
                validators.clean_keyword(digest_seed)
                if digest_seed is not None
                else current.digest_seed
            )
        except validators.ValidationError as exc:
            await interaction.response.send_message(
                embed=embeds.error_embed(str(exc), title="Invalid input"),
                ephemeral=True,
            )
            return

        new_channel_id = (
            digest_channel.id if digest_channel is not None else current.digest_channel_id
        )

        updated = GuildSettings(
            guild_id=interaction.guild_id,
            default_geo=new_geo,
            default_timeframe=new_tf,
            digest_channel_id=new_channel_id,
            digest_seed=new_seed,
        )
        await self.repo.upsert(updated)

        await interaction.response.send_message(
            embed=embeds.info_embed(
                (
                    f"Region: `{updated.default_geo or 'Worldwide'}`\n"
                    f"Timeframe: `{updated.default_timeframe}`\n"
                    f"Digest seed: `{updated.digest_seed}`\n"
                    f"Digest channel: "
                    + (f"<#{updated.digest_channel_id}>"
                       if updated.digest_channel_id else "_none_")
                ),
                title="Settings saved",
            ),
            ephemeral=True,
        )

    # ------ /trends_settings disable_digest --------------------------
    @settings_group.command(
        name="disable_digest",
        description="Stop posting the daily digest in this server.",
    )
    async def disable_digest(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        current = await self.repo.get_or_default(interaction.guild_id)
        if current.digest_channel_id is None:
            await interaction.response.send_message(
                "Daily digest isn't enabled here.", ephemeral=True
            )
            return
        await self.repo.upsert(
            GuildSettings(
                guild_id=current.guild_id,
                default_geo=current.default_geo,
                default_timeframe=current.default_timeframe,
                digest_channel_id=None,
                digest_seed=current.digest_seed,
            )
        )
        await interaction.response.send_message(
            "Daily digest disabled.", ephemeral=True
        )


async def setup_cog(bot: commands.Bot, repo: GuildSettingsRepo) -> None:
    await bot.add_cog(SettingsCog(bot, repo))
