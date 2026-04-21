"""YouTube Data API v3 client.

This is a *separate* data source from :mod:`bot.services.trends_service`:

- ``TrendsService`` (via pytrends) reports *relative search interest* from
  Google Trends. Numbers are 0-100 and refer to how often users *searched*
  for a term. No counts, no videos, no per-video metadata.
- ``YouTubeDataService`` (this module) reports *actual videos* on YouTube,
  including view counts, like counts, channel, and thumbnails — sourced from
  the official YouTube Data API v3.

Both services live side-by-side; the cog layer chooses which to call based on
what the user asked for. This keeps the two fundamentally different data
shapes cleanly separated.

The service is optional: if ``YOUTUBE_API_KEY`` is not set, commands that
depend on it raise :class:`YouTubeAPINotConfigured` which the cog renders as
a friendly embed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import aiohttp

from bot.services.cache import TTLKeyCache, make_key

log = logging.getLogger(__name__)

_API_BASE = "https://www.googleapis.com/youtube/v3"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class YouTubeAPIError(Exception):
    """Base class for YouTube Data API errors."""


class YouTubeAPINotConfigured(YouTubeAPIError):
    """``YOUTUBE_API_KEY`` is missing."""


class YouTubeAPIUnavailable(YouTubeAPIError):
    """Network error or unexpected response."""


class YouTubeQuotaExceeded(YouTubeAPIError):
    """YouTube returned 403 quotaExceeded."""


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class YouTubeVideo:
    video_id: str
    title: str
    channel: str
    published_at: str
    view_count: Optional[int]
    like_count: Optional[int]
    comment_count: Optional[int]
    thumbnail_url: Optional[str]

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class YouTubeDataService:
    """Wraps the YouTube Data API v3 REST endpoints we actually use."""

    def __init__(
        self,
        api_key: Optional[str],
        *,
        session: Optional[aiohttp.ClientSession] = None,
        cache: Optional[TTLKeyCache] = None,
    ) -> None:
        self._api_key = api_key
        self._external_session = session is not None
        self._session: Optional[aiohttp.ClientSession] = session
        self._cache = cache

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def close(self) -> None:
        if self._session is not None and not self._external_session:
            await self._session.close()
            self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        return self._session

    # -- top-level convenience ---------------------------------------------

    async def top_videos(
        self,
        *,
        query: str,
        region_code: str = "",
        max_results: int = 10,
    ) -> list[YouTubeVideo]:
        """Return popular videos matching ``query``.

        Strategy: use ``search.list`` with ``order=viewCount`` to get IDs,
        then ``videos.list`` to enrich with stats. This is the canonical
        pattern recommended by the YouTube Data API docs.
        """
        if not self.is_configured:
            raise YouTubeAPINotConfigured(
                "YOUTUBE_API_KEY is not set — enable it to use YouTube Data API features."
            )

        cache_key = make_key("yt_top_videos", query, region_code, max_results)
        if self._cache is not None:
            hit = await self._cache.get(cache_key)
            if hit is not None:
                return hit

        ids = await self._search_ids(
            query=query, region_code=region_code, max_results=max_results
        )
        if not ids:
            return []
        videos = await self._video_details(ids)
        if self._cache is not None:
            await self._cache.set(cache_key, videos)
        return videos

    # -- raw-ish endpoints --------------------------------------------------

    async def _search_ids(
        self, *, query: str, region_code: str, max_results: int
    ) -> list[str]:
        params = {
            "key": self._api_key,
            "part": "id",
            "type": "video",
            "order": "viewCount",
            "q": query,
            "maxResults": max(1, min(50, max_results)),
        }
        if region_code:
            params["regionCode"] = region_code
        payload = await self._get_json("/search", params=params)
        return [
            item["id"]["videoId"]
            for item in payload.get("items", [])
            if item.get("id", {}).get("videoId")
        ]

    async def _video_details(self, ids: Sequence[str]) -> list[YouTubeVideo]:
        if not ids:
            return []
        params = {
            "key": self._api_key,
            "part": "snippet,statistics",
            "id": ",".join(ids),
            "maxResults": 50,
        }
        payload = await self._get_json("/videos", params=params)
        videos: list[YouTubeVideo] = []
        for item in payload.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            thumbs = snippet.get("thumbnails", {})
            thumb = (thumbs.get("high") or thumbs.get("default") or {}).get("url")
            videos.append(
                YouTubeVideo(
                    video_id=item["id"],
                    title=snippet.get("title", "").strip(),
                    channel=snippet.get("channelTitle", "").strip(),
                    published_at=snippet.get("publishedAt", ""),
                    view_count=_safe_int(stats.get("viewCount")),
                    like_count=_safe_int(stats.get("likeCount")),
                    comment_count=_safe_int(stats.get("commentCount")),
                    thumbnail_url=thumb,
                )
            )
        return videos

    # -- HTTP plumbing ------------------------------------------------------

    async def _get_json(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        session = await self._get_session()
        url = f"{_API_BASE}{path}"
        try:
            async with session.get(url, params=params) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 200:
                    return data
                reason = _extract_reason(data)
                if resp.status == 403 and reason == "quotaExceeded":
                    raise YouTubeQuotaExceeded(
                        "YouTube Data API daily quota exceeded."
                    )
                log.warning(
                    "YouTube API error %s on %s: %s", resp.status, path, data
                )
                raise YouTubeAPIUnavailable(
                    f"YouTube API returned HTTP {resp.status} ({reason or 'unknown'})."
                )
        except aiohttp.ClientError as exc:
            log.warning("YouTube API client error: %s", exc)
            raise YouTubeAPIUnavailable(
                "Couldn't reach the YouTube Data API. Please try again shortly."
            ) from exc


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_reason(payload: Any) -> str:
    try:
        errors = payload.get("error", {}).get("errors", [])
        if errors:
            return str(errors[0].get("reason", ""))
    except AttributeError:
        pass
    return ""
