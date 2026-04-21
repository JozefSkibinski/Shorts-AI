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


def paged_related_trends_embeds(
    keyword: str,
    geo: str,
    timeframe: str,
    top: Sequence[Mapping[str, object]],
    rising: Sequence[Mapping[str, object]],
    *,
    per_page: int = 10,
) -> list[discord.Embed]:
    """Split a related-trends result into multiple embeds for pagination."""
    def _chunk(rows: Sequence[Mapping[str, object]]) -> list[list[Mapping[str, object]]]:
        if not rows:
            return [[]]
        return [list(rows[i : i + per_page]) for i in range(0, len(rows), per_page)]

    top_chunks = _chunk(top)
    rising_chunks = _chunk(rising)
    pages_count = max(len(top_chunks), len(rising_chunks))
    embeds: list[discord.Embed] = []
    for i in range(pages_count):
        top_slice = top_chunks[i] if i < len(top_chunks) else []
        rising_slice = rising_chunks[i] if i < len(rising_chunks) else []
        embed = related_trends_embed(
            keyword=keyword,
            geo=geo,
            timeframe=timeframe,
            top=top_slice,
            rising=rising_slice,
            limit=per_page,
        )
        embed.title = f"{embed.title} — page {i + 1}/{pages_count}"
        embeds.append(embed)
    return embeds


def youtube_videos_embed(
    query: str,
    region: str,
    videos: Sequence[Mapping[str, object]],
) -> discord.Embed:
    """Build the embed returned by ``/youtube_videos`` (YouTube Data API)."""
    embed = discord.Embed(
        title=f"Top YouTube videos — {query}",
        description=(
            "Real YouTube videos ordered by view count. "
            "This uses the YouTube Data API, not Google Trends."
        ),
        color=COLOR_PRIMARY,
        timestamp=datetime.now(timezone.utc),
    )
    if not videos:
        embed.add_field(name="No data", value="No videos matched.", inline=False)
    else:
        for i, v in enumerate(videos, start=1):
            title = str(v.get("title", "?"))
            channel = str(v.get("channel", "?"))
            views = v.get("view_count")
            likes = v.get("like_count")
            url = v.get("url") or ""
            bits = [f"👤 {channel}"]
            if views is not None:
                bits.append(f"👁 {int(views):,}")
            if likes is not None:
                bits.append(f"👍 {int(likes):,}")
            embed.add_field(
                name=f"{i}. {title[:240]}",
                value=f"{' • '.join(bits)}\n{url}",
                inline=False,
            )
    footer_region = region or "Worldwide"
    embed.set_footer(
        text=f"Source: YouTube Data API v3 • Region: {footer_region}"
    )
    return embed


def diff_embed(
    keyword: str,
    geo: str,
    timeframe: str,
    diff: Mapping[str, Sequence[str]],
    *,
    previous_timestamp: str,
) -> discord.Embed:
    """Embed comparing current vs. previous snapshot of a related-queries result."""
    embed = discord.Embed(
        title=f"Week-over-week change for: {keyword}",
        description=f"Compared against snapshot taken at `{previous_timestamp}`.",
        color=COLOR_SUCCESS,
        timestamp=datetime.now(timezone.utc),
    )

    def _fmt(items: Sequence[str], empty: str) -> str:
        if not items:
            return f"_{empty}_"
        return "\n".join(f"• {q}" for q in items[:15])

    embed.add_field(name="🆕 New queries", value=_fmt(diff.get("new", []), "none"),
                    inline=False)
    embed.add_field(name="📉 Dropped off", value=_fmt(diff.get("dropped", []), "none"),
                    inline=False)
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
