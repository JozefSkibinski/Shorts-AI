"""Thin async-friendly wrapper around ``pytrends`` + an in-memory TTL cache.

``pytrends`` is a synchronous library that internally uses ``requests``. We
wrap every blocking call in :func:`asyncio.to_thread` so the Discord bot's
event loop is never stalled while Google Trends responds.

The wrapper also centralises error translation: pytrends raises bare
``requests`` exceptions and ``TooManyRequestsError`` which we convert into
the custom exceptions below so the command layer can render friendly
embeds without knowing any implementation details.

Results are cached in :class:`TTLKeyCache` instances so repeated queries
within the TTL window don't hit Google Trends at all.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
from pytrends.exceptions import ResponseError, TooManyRequestsError
from pytrends.request import TrendReq
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import ReadTimeout, RequestException

from bot.services.cache import TTLKeyCache, make_key

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class TrendsServiceError(Exception):
    """Base class for all errors raised by the service layer."""


class TrendsRateLimited(TrendsServiceError):
    """Google Trends returned HTTP 429."""


class TrendsUnavailable(TrendsServiceError):
    """Network error or unexpected Google Trends response."""


class TrendsNoData(TrendsServiceError):
    """The query succeeded but returned no usable data."""


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelatedQueries:
    """Normalised shape returned by :meth:`TrendsService.related_queries`."""

    top: List[Dict[str, Any]]
    rising: List[Dict[str, Any]]


@dataclass(frozen=True)
class Suggestion:
    """A keyword suggestion as returned by Google Trends' suggestions API."""

    mid: str    # canonical topic id, e.g. "/m/0bs5k8r"
    title: str
    type: str   # "Topic", "Musician", etc.


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TrendsService:
    """Async wrapper around a single ``TrendReq`` instance + TTL cache."""

    YOUTUBE_GPROP = "youtube"

    def __init__(
        self,
        *,
        timeout_connect: int = 10,
        timeout_read: int = 25,
        proxy: Optional[str] = None,
        max_concurrency: int = 2,
        result_cache: Optional[TTLKeyCache] = None,
        suggestion_cache: Optional[TTLKeyCache] = None,
    ) -> None:
        proxies = [proxy] if proxy else []
        # ``hl`` is the UI language; ``tz`` is the timezone offset in minutes
        # (360 == US Central, which matches pytrends' own default).
        self._client = TrendReq(
            hl="en-US",
            tz=360,
            timeout=(timeout_connect, timeout_read),
            proxies=proxies,
            retries=2,
            backoff_factor=0.5,
        )
        self._sem = asyncio.Semaphore(max_concurrency)
        self._result_cache = result_cache
        self._suggestion_cache = suggestion_cache

    # --- public API --------------------------------------------------------

    async def related_queries(
        self,
        keyword: str,
        *,
        geo: str = "",
        timeframe: str = "today 3-m",
    ) -> RelatedQueries:
        """Top + rising related YouTube searches for ``keyword``."""
        key = make_key("related", keyword, geo, timeframe)

        async def fetch() -> RelatedQueries:
            raw = await self._run(
                self._related_queries_sync, keyword, geo, timeframe
            )
            top = _safe_records(raw.get(keyword, {}).get("top"))
            rising = _safe_records(raw.get(keyword, {}).get("rising"))
            if not top and not rising:
                raise TrendsNoData(
                    f"No related queries for `{keyword}` in region "
                    f"`{geo or 'WW'}` / `{timeframe}`."
                )
            return RelatedQueries(top=top, rising=rising)

        return await self._cached(self._result_cache, key, fetch)

    async def top_trends(
        self,
        *,
        seed: str,
        geo: str = "",
        timeframe: str = "now 7-d",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Top trending YouTube queries for a region (see docs for caveats)."""
        related = await self.related_queries(seed, geo=geo, timeframe=timeframe)
        rows = related.rising or related.top
        return rows[:limit]

    async def compare_keywords(
        self,
        keywords: Sequence[str],
        *,
        geo: str = "",
        timeframe: str = "today 3-m",
    ) -> Tuple[pd.DataFrame, Dict[str, float], str]:
        """Return ``(interest_over_time_df, averages, winner)``."""
        if not 2 <= len(keywords) <= 5:
            raise TrendsServiceError("compare_keywords requires 2-5 keywords.")

        key = make_key("compare", list(keywords), geo, timeframe)

        async def fetch():
            frame = await self._run(
                self._interest_over_time_sync, list(keywords), geo, timeframe
            )
            if frame is None or frame.empty:
                raise TrendsNoData(
                    "Google Trends returned no interest-over-time data for those keywords."
                )
            data = frame.drop(
                columns=[c for c in ("isPartial",) if c in frame.columns]
            )
            averages = {
                kw: float(data[kw].mean()) for kw in keywords if kw in data.columns
            }
            if not averages:
                raise TrendsNoData("No comparable data was returned for any keyword.")
            winner = max(averages, key=averages.get)
            return data, averages, winner

        return await self._cached(self._result_cache, key, fetch)

    async def suggestions(self, keyword: str) -> List[Suggestion]:
        """Return keyword suggestions from Google Trends.

        This is a best-effort endpoint; if it fails we return an empty list
        rather than an exception — suggestions are a nicety, not critical.
        """
        cleaned = keyword.strip()
        if not cleaned:
            return []

        key = make_key("suggest", cleaned)

        async def fetch() -> List[Suggestion]:
            try:
                raw = await self._run(self._suggestions_sync, cleaned)
            except TrendsServiceError:
                return []
            return [
                Suggestion(
                    mid=str(s.get("mid", "")),
                    title=str(s.get("title", "")).strip(),
                    type=str(s.get("type", "")).strip(),
                )
                for s in (raw or [])
                if s.get("title")
            ]

        return await self._cached(self._suggestion_cache, key, fetch, default=[])

    # --- cache helpers -----------------------------------------------------

    async def _cached(self, cache: Optional[TTLKeyCache], key, factory, *, default=None):
        if cache is None:
            return await factory()
        cached = await cache.get(key)
        if cached is not None:
            return cached
        try:
            value = await factory()
        except TrendsNoData:
            # Don't cache empty results — give Google a chance next time.
            raise
        if value is None:
            return default
        await cache.set(key, value)
        return value

    # --- private helpers ---------------------------------------------------

    async def _run(self, fn, *args):
        """Run a blocking pytrends call off the event loop and translate errors."""
        async with self._sem:
            try:
                return await asyncio.to_thread(fn, *args)
            except TooManyRequestsError as exc:
                log.warning("Google Trends rate limited: %s", exc)
                raise TrendsRateLimited(
                    "Google Trends is rate-limiting us right now. Try again in a minute."
                ) from exc
            except (ReadTimeout, ReqConnectionError) as exc:
                log.warning("Google Trends network error: %s", exc)
                raise TrendsUnavailable(
                    "Couldn't reach Google Trends. Please try again shortly."
                ) from exc
            except ResponseError as exc:
                log.warning("Google Trends response error: %s", exc)
                raise TrendsUnavailable(
                    f"Google Trends returned an error: {exc}"
                ) from exc
            except RequestException as exc:
                log.exception("Unexpected requests error talking to Google Trends")
                raise TrendsUnavailable(
                    "Unexpected network error while contacting Google Trends."
                ) from exc
            except Exception as exc:  # pragma: no cover - safety net
                log.exception("Unexpected pytrends failure")
                raise TrendsUnavailable(
                    "Unexpected error while contacting Google Trends."
                ) from exc

    def _related_queries_sync(
        self, keyword: str, geo: str, timeframe: str
    ) -> Dict[str, Any]:
        self._client.build_payload(
            kw_list=[keyword],
            timeframe=timeframe,
            geo=geo,
            gprop=self.YOUTUBE_GPROP,
        )
        return self._client.related_queries()

    def _interest_over_time_sync(
        self, keywords: List[str], geo: str, timeframe: str
    ) -> pd.DataFrame:
        self._client.build_payload(
            kw_list=keywords,
            timeframe=timeframe,
            geo=geo,
            gprop=self.YOUTUBE_GPROP,
        )
        return self._client.interest_over_time()

    def _suggestions_sync(self, keyword: str) -> list[dict[str, Any]]:
        return self._client.suggestions(keyword=keyword)


def _safe_records(df: Any) -> List[Dict[str, Any]]:
    """Convert a pytrends-returned DataFrame/None into a plain list of dicts."""
    if df is None:
        return []
    if isinstance(df, pd.DataFrame):
        if df.empty:
            return []
        return df.to_dict(orient="records")
    return list(df) if isinstance(df, list) else []
