"""Input validation helpers for slash-command parameters."""

from __future__ import annotations

import re
from typing import List, Tuple


# Google Trends accepts timeframes like "now 1-d", "today 1-m", "today 12-m",
# "today 5-y", or an explicit "YYYY-MM-DD YYYY-MM-DD" range. We expose a
# friendly, curated set via the slash-command choices in the cog; this regex
# is only used as a defensive check.
_TIMEFRAME_RE = re.compile(
    r"^("
    r"now \d+-[HdHmY]|"
    r"today \d+-[dmy]|"
    r"today \d+-[dmy]|"
    r"\d{4}-\d{2}-\d{2} \d{4}-\d{2}-\d{2}|"
    r"all"
    r")$",
    re.IGNORECASE,
)

# ISO-3166 alpha-2 country codes are two uppercase letters. Google Trends
# treats empty string as "worldwide".
_GEO_RE = re.compile(r"^[A-Z]{2}$")


class ValidationError(ValueError):
    """Raised when a user-supplied parameter is invalid."""


def clean_keyword(raw: str, *, max_len: int = 100) -> str:
    """Trim, collapse whitespace and enforce a length cap."""
    if raw is None:
        raise ValidationError("Keyword is required.")
    cleaned = re.sub(r"\s+", " ", raw).strip()
    if not cleaned:
        raise ValidationError("Keyword cannot be empty.")
    if len(cleaned) > max_len:
        raise ValidationError(f"Keyword is too long (max {max_len} characters).")
    return cleaned


def parse_keyword_list(raw: str, *, min_items: int = 2, max_items: int = 5) -> List[str]:
    """Split a comma-separated string into a clean list of keywords."""
    if not raw:
        raise ValidationError("Please provide keywords separated by commas.")
    parts = [clean_keyword(p) for p in raw.split(",") if p.strip()]
    if len(parts) < min_items:
        raise ValidationError(f"Please provide at least {min_items} keywords.")
    if len(parts) > max_items:
        raise ValidationError(f"Please provide at most {max_items} keywords.")
    # Google Trends is case-insensitive but de-duplicates exact matches only,
    # so normalise casing when checking for duplicates.
    seen_lower = {p.lower() for p in parts}
    if len(seen_lower) != len(parts):
        raise ValidationError("Duplicate keywords are not allowed.")
    return parts


def validate_geo(raw: str | None) -> str:
    """Return a normalised geo code or an empty string for worldwide."""
    if raw is None:
        return ""
    cleaned = raw.strip().upper()
    if not cleaned:
        return ""
    if not _GEO_RE.match(cleaned):
        raise ValidationError(
            f"Invalid region code `{raw}`. Use ISO alpha-2 codes like `US`, `GB`, `IN`."
        )
    return cleaned


def validate_timeframe(raw: str | None) -> str:
    """Return a Google-Trends-compatible timeframe string."""
    if not raw:
        return "today 3-m"  # sensible default
    cleaned = raw.strip().lower()
    if not _TIMEFRAME_RE.match(cleaned):
        raise ValidationError(
            "Invalid time range. Expected values like `now 1-d`, `today 1-m`, "
            "`today 12-m`, `today 5-y`, or an explicit `YYYY-MM-DD YYYY-MM-DD` range."
        )
    return cleaned


def clamp_limit(value: int | None, *, default: int = 10, lo: int = 1, hi: int = 25) -> int:
    """Clamp an integer into a sane range and supply a default."""
    if value is None:
        return default
    return max(lo, min(hi, int(value)))


def keyword_property_pair(keyword: str, youtube_only: bool) -> Tuple[str, str]:
    """Return a ``(keyword, gprop)`` pair for pytrends.

    Google Trends exposes a ``gprop`` (Google property) filter; ``"youtube"``
    restricts the query to YouTube search data, which is what we want for
    this bot. Setting it to the empty string means "all of Google Search".
    """
    return keyword, "youtube" if youtube_only else ""
