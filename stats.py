"""Compute ranked statistics for scraped YouTube Shorts.

Reads ``output/shorts.jsonl`` (one JSON object per line, written by the
scraper) and produces a human-readable ``output/stats.txt`` ranking:

- Hashtags by total views, by avg views, by count
- yt-dlp keyword tags by total / avg views
- Channels by total views, avg views, video count
- Duration buckets (which lengths perform best)
- Hook vocabulary by avg views (which words appear in the best hooks)

Run automatically at the end of every scrape, or standalone with
``python main.py --stats-only``.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_records(jsonl_path: Path) -> list[dict]:
    rows: list[dict] = []
    if not jsonl_path.exists():
        return rows
    with jsonl_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "to", "of", "in", "on",
    "for", "with", "is", "are", "was", "were", "be", "been", "being",
    "i", "you", "he", "she", "it", "we", "they", "this", "that", "these",
    "those", "my", "your", "his", "her", "its", "our", "their", "me",
    "him", "us", "them", "do", "does", "did", "doing", "have", "has",
    "had", "will", "would", "can", "could", "should", "shall", "may",
    "might", "must", "so", "as", "at", "by", "from", "up", "down", "out",
    "off", "all", "any", "some", "no", "not", "only", "own", "just",
    "than", "too", "very", "what", "when", "where", "who", "how", "why",
    "there", "here", "now", "then", "ll", "ve", "re", "s", "t", "d", "m",
    "like", "im", "dont", "youre", "thats", "yeah", "ok", "okay", "uh",
    "um", "well", "go", "got", "get", "make", "made", "see", "look",
    "know", "think", "want", "going", "gonna", "wanna", "really", "even",
    "one", "two", "three", "first", "last", "right", "still", "let",
    "us", "way", "back",
}


def _bucketize_duration(d: float | None) -> str:
    if not d or d <= 0:
        return "unknown"
    if d <= 15:
        return "0-15s"
    if d <= 30:
        return "16-30s"
    if d <= 45:
        return "31-45s"
    if d <= 60:
        return "46-60s"
    return "60s+"


def aggregate_groups(rows: list[dict]) -> dict[str, dict[str, list[int]]]:
    """Build {dimension: {key: [view_count, view_count, ...]}}."""
    groups: dict[str, dict[str, list[int]]] = {
        "hashtags": defaultdict(list),
        "keywords": defaultdict(list),
        "channels": defaultdict(list),
        "duration": defaultdict(list),
        "hook_words": defaultdict(list),
    }

    for row in rows:
        views = int(row.get("view_count") or 0)
        if views <= 0:
            continue

        for tag in row.get("hashtags") or []:
            if tag:
                groups["hashtags"][tag.lower()].append(views)

        # yt-dlp keywords aren't always saved; skip if absent
        for kw in row.get("keywords") or []:
            if kw:
                groups["keywords"][kw.lower()].append(views)

        ch = (row.get("channel") or "").strip()
        if ch:
            groups["channels"][ch].append(views)

        groups["duration"][_bucketize_duration(row.get("duration_seconds"))].append(
            views
        )

        hook = (row.get("hook") or "").lower()
        if hook:
            words = re.findall(r"[a-z']{2,}", hook)
            seen: set[str] = set()
            for w in words:
                if w in _STOPWORDS or len(w) < 3:
                    continue
                if w in seen:
                    continue
                seen.add(w)
                groups["hook_words"][w].append(views)

    # convert defaultdicts back to plain dicts for cleanliness
    return {k: dict(v) for k, v in groups.items()}


# ---------------------------------------------------------------------------
# Ranking + table formatting
# ---------------------------------------------------------------------------


def _rank(
    items: dict[str, list[int]],
    sort_key: str = "total",
    min_count: int = 1,
    limit: int = 20,
) -> list[tuple[str, int, int, int, int]]:
    """Return list of (key, total, mean, median, count) sorted by sort_key."""
    rows = []
    for key, views in items.items():
        if len(views) < min_count:
            continue
        total = sum(views)
        mean = total // len(views)
        median = int(statistics.median(views))
        rows.append((key, total, mean, median, len(views)))

    if sort_key == "total":
        rows.sort(key=lambda r: -r[1])
    elif sort_key == "mean":
        rows.sort(key=lambda r: -r[2])
    elif sort_key == "count":
        rows.sort(key=lambda r: -r[4])
    else:
        rows.sort(key=lambda r: -r[1])

    return rows[:limit]


def _fmt_int(n: int) -> str:
    return f"{n:>14,}"


def _fmt_table(
    title: str,
    header_label: str,
    rows: list[tuple[str, int, int, int, int]],
) -> str:
    if not rows:
        return f"{title}\n  (no data)\n"
    lines = [title]
    lines.append(
        f"  {'#':>3}  {'TOTAL VIEWS':>14}  {'AVG':>14}  {'MEDIAN':>14}  {'N':>4}  {header_label}"
    )
    for i, (key, total, mean, median, n) in enumerate(rows, 1):
        lines.append(
            f"  {i:>3}  {_fmt_int(total)}  {_fmt_int(mean)}  {_fmt_int(median)}  {n:>4}  {key}"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public report
# ---------------------------------------------------------------------------


def build_report(rows: list[dict]) -> str:
    if not rows:
        return "No records to analyze. Run the scraper first.\n"

    total_views = sum(int(r.get("view_count") or 0) for r in rows)
    durations = [
        float(r["duration_seconds"])
        for r in rows
        if r.get("duration_seconds")
    ]
    avg_dur = sum(durations) / len(durations) if durations else 0.0

    groups = aggregate_groups(rows)

    out: list[str] = []
    out.append("=" * 72)
    out.append("SHORTS STATISTICS")
    out.append("=" * 72)
    out.append(f"Videos analyzed:  {len(rows):,}")
    out.append(f"Total views:      {total_views:,}")
    out.append(f"Average duration: {avg_dur:.1f}s")
    out.append("")

    out.append(_fmt_table(
        "TOP HASHTAGS BY TOTAL VIEWS",
        "hashtag",
        _rank(groups["hashtags"], sort_key="total", limit=25),
    ))
    out.append(_fmt_table(
        "TOP HASHTAGS BY AVG VIEWS (min 3 videos)",
        "hashtag",
        _rank(groups["hashtags"], sort_key="mean", min_count=3, limit=20),
    ))

    if any(groups["keywords"].values()):
        out.append(_fmt_table(
            "TOP KEYWORDS (yt-dlp tags) BY TOTAL VIEWS",
            "keyword",
            _rank(groups["keywords"], sort_key="total", limit=25),
        ))

    out.append(_fmt_table(
        "TOP CHANNELS BY TOTAL VIEWS",
        "channel",
        _rank(groups["channels"], sort_key="total", limit=20),
    ))
    out.append(_fmt_table(
        "TOP CHANNELS BY AVG VIEWS (min 2 videos)",
        "channel",
        _rank(groups["channels"], sort_key="mean", min_count=2, limit=20),
    ))

    out.append(_fmt_table(
        "DURATION BUCKETS",
        "bucket",
        _rank(groups["duration"], sort_key="mean", limit=10),
    ))

    out.append(_fmt_table(
        "TOP HOOK WORDS BY AVG VIEWS (min 3 hooks)",
        "word",
        _rank(groups["hook_words"], sort_key="mean", min_count=3, limit=30),
    ))

    return "\n".join(out)


def write_report(jsonl_path: Path, stats_path: Path) -> int:
    """Read jsonl, write stats to stats_path. Returns number of records."""
    rows = load_records(jsonl_path)
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(build_report(rows), encoding="utf-8")
    return len(rows)
