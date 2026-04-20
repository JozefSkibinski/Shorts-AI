"""Helpers that turn pytrends data into Discord embeds."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

import discord


# Accent colours. Discord shows a coloured bar on the left edge of an embed.
COLOR_PRIMARY = 0xFF0000   # YouTube red
COLOR_SUCCESS = 0x57F287
COLOR_WARNING = 0xFEE75C
COLOR_ERROR = 0xED4245


def _footer_text(geo: str, timeframe: str) -> str:
    region = geo or "Worldwide"
    return f"Source: Google Trends • Region: {region} • Timeframe: {timeframe}"


def related_trends_embed(
    keyword: str,
    geo: str,
    timeframe: str,
    top: Sequence[Mapping[str, object]],
    rising: Sequence[Mapping[str, object]],
    *,
    limit: int = 10,
) -> discord.Embed:
    """Build the embed returned by ``/youtube_trends``."""
    embed = discord.Embed(
        title=f"YouTube trends related to: {keyword}",
        description=(
            "Queries that YouTube users have searched for alongside your keyword.\n"
            "**Top** = consistently popular. **Rising** = fastest growing."
        ),
        color=COLOR_PRIMARY,
        timestamp=datetime.now(timezone.utc),
    )

    def _fmt(rows: Sequence[Mapping[str, object]], value_key: str) -> str:
        if not rows:
            return "_No data available._"
        lines = []
        for i, row in enumerate(rows[:limit], start=1):
            query = str(row.get("query", "?"))
            value = row.get(value_key)
            suffix = f" — `{value}`" if value is not None else ""
            lines.append(f"**{i}.** {query}{suffix}")
        return "\n".join(lines)

    embed.add_field(name="🔝 Top related", value=_fmt(top, "value"), inline=False)
    embed.add_field(name="📈 Rising related", value=_fmt(rising, "value"), inline=False)
    embed.set_footer(text=_footer_text(geo, timeframe))
    return embed


def top_trends_embed(
    geo: str,
    timeframe: str,
    rows: Sequence[Mapping[str, object]],
    *,
    seed: str,
) -> discord.Embed:
    """Build the embed returned by ``/top_youtube_trends``."""
    embed = discord.Embed(
        title=f"Top YouTube trend queries — {geo or 'Worldwide'}",
        description=(
            "Approximation built from Google Trends' YouTube filter. See the "
            "bot README for details on how this list is derived."
        ),
        color=COLOR_PRIMARY,
        timestamp=datetime.now(timezone.utc),
    )

    if not rows:
        embed.add_field(
            name="No data",
            value="Google Trends returned nothing for this region and timeframe.",
            inline=False,
        )
    else:
        lines = []
        for i, row in enumerate(rows, start=1):
            query = str(row.get("query", "?"))
            value = row.get("value")
            suffix = f" — `{value}`" if value is not None else ""
            lines.append(f"**{i}.** {query}{suffix}")
        embed.add_field(
            name=f"Trending (seeded from `{seed}`)",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text=_footer_text(geo, timeframe))
    return embed


def compare_trends_embed(
    keywords: Sequence[str],
    averages: Mapping[str, float],
    winner: str,
    geo: str,
    timeframe: str,
) -> discord.Embed:
    """Build the text embed returned by ``/compare_trends`` (chart is separate)."""
    embed = discord.Embed(
        title="YouTube trend comparison",
        description="Average interest over the selected timeframe (0–100 scale).",
        color=COLOR_SUCCESS,
        timestamp=datetime.now(timezone.utc),
    )

    lines = []
    for kw in keywords:
        score = averages.get(kw)
        if score is None:
            lines.append(f"• **{kw}** — _no data_")
        else:
            marker = " 🏆" if kw == winner else ""
            lines.append(f"• **{kw}** — `{score:.1f}`{marker}")

    embed.add_field(name="Results", value="\n".join(lines) or "_empty_", inline=False)
    embed.add_field(
        name="Most popular",
        value=f"**{winner}**" if winner else "_inconclusive_",
        inline=False,
    )
    embed.set_footer(text=_footer_text(geo, timeframe))
    return embed


def error_embed(message: str, *, title: str = "Something went wrong") -> discord.Embed:
    """A consistent red embed for surface-level errors."""
    return discord.Embed(title=title, description=message, color=COLOR_ERROR)


def info_embed(message: str, *, title: str = "Heads up") -> discord.Embed:
    return discord.Embed(title=title, description=message, color=COLOR_WARNING)


def iter_keyword_field_chunks(
    lines: Iterable[str], *, chunk_chars: int = 1000
) -> list[str]:
    """Split long text into <=1024-char chunks to respect Discord field limits."""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        line_len = len(line) + 1
        if size + line_len > chunk_chars and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += line_len
    if buf:
        chunks.append("\n".join(buf))
    return chunks
