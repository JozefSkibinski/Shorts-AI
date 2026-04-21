"""Small TTL cache wrapper used by the service layer.

Backed by :class:`cachetools.TTLCache`. Two instances are created by the bot:
one short-lived cache for query results, and a longer-lived cache for
keyword suggestions (which change rarely). Keeping them separate means a
burst of trend queries can't evict suggestion data and vice versa.

The class also exposes a lightweight :func:`stats` helper for the healthcheck
endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Hashable, Optional

from cachetools import TTLCache

log = logging.getLogger(__name__)


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    size: int = 0
    maxsize: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "size": self.size,
            "maxsize": self.maxsize,
        }


class TTLKeyCache:
    """Thin async-friendly wrapper around ``cachetools.TTLCache``."""

    def __init__(self, *, maxsize: int, ttl: int) -> None:
        self._cache: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    def _count_get(self, key: Hashable) -> Any:
        # Called under lock; TTLCache itself isn't thread-safe for expiry
        # cleanup, but we serialise all reads/writes through ``self._lock``.
        try:
            value = self._cache[key]
            self._hits += 1
            return value
        except KeyError:
            self._misses += 1
            return None

    async def get(self, key: Hashable) -> Any:
        async with self._lock:
            return self._count_get(key)

    async def set(self, key: Hashable, value: Any) -> None:
        async with self._lock:
            self._cache[key] = value

    async def get_or_set(
        self, key: Hashable, factory: Callable[[], "asyncio.Future | Any"]
    ) -> Any:
        """Return cached value or compute + cache it.

        ``factory`` must be an async callable returning the value. We release
        the lock while it runs so a slow Google Trends round-trip doesn't
        block unrelated cache lookups.
        """
        async with self._lock:
            cached = self._count_get(key)
            if cached is not None:
                return cached

        value = await factory()
        if value is not None:
            async with self._lock:
                self._cache[key] = value
        return value

    async def invalidate(self, key: Hashable) -> None:
        async with self._lock:
            self._cache.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()

    def stats(self) -> CacheStats:
        return CacheStats(
            hits=self._hits,
            misses=self._misses,
            size=len(self._cache),
            maxsize=self._cache.maxsize,
        )


def make_key(*parts: Any) -> tuple:
    """Deterministic hashable key from heterogeneous parts.

    Lists/tuples of keywords are sorted so ``(a, b)`` and ``(b, a)`` map to the
    same cache entry. Strings are lower-cased.
    """
    normalised: list[Any] = []
    for p in parts:
        if isinstance(p, (list, tuple)):
            normalised.append(tuple(sorted(str(x).lower() for x in p)))
        elif isinstance(p, str):
            normalised.append(p.lower())
        else:
            normalised.append(p)
    return tuple(normalised)
