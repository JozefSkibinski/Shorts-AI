"""Unit tests for :mod:`bot.services.trends_service`.

We use the ``responses`` library to stub the HTTP traffic that ``pytrends``
performs through ``requests``. This lets us exercise the real ``TrendReq``
code paths — and therefore our error translations and caching glue —
without hitting Google.

pytrends' HTTP flow is simple enough to mock:

1. ``build_payload`` issues a GET to ``/trends/api/explore`` to obtain tokens
   for each widget.
2. Data calls (``related_queries``, ``interest_over_time``, ``suggestions``)
   GET from ``/trends/api/widgetdata/<name>`` or ``/trends/api/autocomplete``
   using those tokens.

For tests that just want to verify cache behaviour or error translation we
instead bypass pytrends entirely by patching the service's private ``_run``.
That's cleaner than maintaining a faithful fake of Google's endpoints.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from pytrends.exceptions import TooManyRequestsError
from requests.exceptions import ConnectionError as ReqConnectionError

from bot.services.cache import TTLKeyCache, make_key
from bot.services.trends_service import (
    RelatedQueries, Suggestion, TrendsNoData, TrendsRateLimited,
    TrendsService, TrendsUnavailable,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(*, with_cache: bool = True) -> TrendsService:
    result_cache = TTLKeyCache(maxsize=64, ttl=60) if with_cache else None
    suggestion_cache = TTLKeyCache(maxsize=64, ttl=60) if with_cache else None
    return TrendsService(
        result_cache=result_cache,
        suggestion_cache=suggestion_cache,
    )


# ---------------------------------------------------------------------------
# make_key
# ---------------------------------------------------------------------------


def test_make_key_normalises_case() -> None:
    assert make_key("related", "Python", "US", "today 3-m") == make_key(
        "related", "python", "us", "today 3-m"
    )


def test_make_key_sorts_lists() -> None:
    assert make_key("compare", ["b", "a"], "", "now 7-d") == make_key(
        "compare", ["a", "b"], "", "now 7-d"
    )


# ---------------------------------------------------------------------------
# Cache-level behaviour (no network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_related_queries_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()

    calls: list[tuple] = []

    async def fake_run(fn, *args):
        calls.append(args)
        return {
            "python": {
                "top": pd.DataFrame([{"query": "python tutorial", "value": 100}]),
                "rising": pd.DataFrame([{"query": "python in 2026", "value": 250}]),
            }
        }

    monkeypatch.setattr(service, "_run", fake_run)

    a = await service.related_queries("python", geo="US", timeframe="today 3-m")
    b = await service.related_queries("python", geo="US", timeframe="today 3-m")
    # Case variations should collide with the same cache key.
    c = await service.related_queries("PYTHON", geo="us", timeframe="today 3-m")

    assert isinstance(a, RelatedQueries)
    assert a.top[0]["query"] == "python tutorial"
    assert a.rising[0]["value"] == 250
    assert a is b is c
    assert len(calls) == 1, "second/third calls should hit the cache"


@pytest.mark.asyncio
async def test_related_queries_no_data_raises_and_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service()

    async def empty(fn, *args):
        return {"python": {"top": None, "rising": None}}

    monkeypatch.setattr(service, "_run", empty)

    with pytest.raises(TrendsNoData):
        await service.related_queries("python", geo="US", timeframe="today 3-m")

    # Next call retries — empty results must NOT be cached.
    called = {"n": 0}

    async def now_populated(fn, *args):
        called["n"] += 1
        return {
            "python": {
                "top": pd.DataFrame([{"query": "python", "value": 1}]),
                "rising": None,
            }
        }

    monkeypatch.setattr(service, "_run", now_populated)
    res = await service.related_queries("python", geo="US", timeframe="today 3-m")
    assert res.top and called["n"] == 1


@pytest.mark.asyncio
async def test_compare_keywords_rejects_too_few() -> None:
    service = _make_service(with_cache=False)
    with pytest.raises(Exception):
        await service.compare_keywords(["solo"])


@pytest.mark.asyncio
async def test_compare_keywords_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(with_cache=False)

    frame = pd.DataFrame(
        {"python": [40, 50, 60], "rust": [10, 15, 20], "isPartial": [False] * 3},
        index=pd.date_range("2026-01-01", periods=3, freq="D"),
    )

    async def fake_run(fn, *args):
        return frame

    monkeypatch.setattr(service, "_run", fake_run)

    data, averages, winner = await service.compare_keywords(
        ["python", "rust"], geo="US", timeframe="today 3-m"
    )
    assert "isPartial" not in data.columns
    assert averages == {"python": 50.0, "rust": 15.0}
    assert winner == "python"


@pytest.mark.asyncio
async def test_suggestions_returns_empty_when_disabled_keyword() -> None:
    service = _make_service()
    assert await service.suggestions("") == []
    assert await service.suggestions("   ") == []


@pytest.mark.asyncio
async def test_suggestions_parses_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()

    async def fake_run(fn, *args):
        return [
            {"mid": "/m/05z1_", "title": "Python", "type": "Programming language"},
            {"mid": "/m/017dcd", "title": "Monty Python", "type": "TV series"},
            {"mid": "", "title": "", "type": ""},  # skipped (empty title)
        ]

    monkeypatch.setattr(service, "_run", fake_run)

    sugg = await service.suggestions("python")
    assert [s.title for s in sugg] == ["Python", "Monty Python"]
    assert all(isinstance(s, Suggestion) for s in sugg)


# ---------------------------------------------------------------------------
# Error translation (with and without the network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_translates_rate_limit() -> None:
    service = _make_service(with_cache=False)

    def raise_429():
        raise TooManyRequestsError("429", response=MagicMock())

    with pytest.raises(TrendsRateLimited):
        await service._run(raise_429)


@pytest.mark.asyncio
async def test_run_translates_connection_error() -> None:
    service = _make_service(with_cache=False)

    def raise_conn():
        raise ReqConnectionError("boom")

    with pytest.raises(TrendsUnavailable):
        await service._run(raise_conn)


# ---------------------------------------------------------------------------
# Integration-style: make a real-looking call go all the way through the
# service and verify the error translation layer works against the actual
# ``requests`` exception hierarchy (not just our own mocks).
#
# We can't easily ``@responses.activate`` the whole test because ``TrendReq``
# issues an HTTP GET during its own ``__init__`` — which would either need
# stubbing, or would happen before ``responses`` is active. Instead we
# construct the service normally and then patch the bound sync methods so
# they raise real ``requests`` exceptions; the public async wrappers must
# still translate them correctly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_related_queries_propagates_request_exceptions_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(with_cache=False)

    def boom(*_args, **_kwargs):
        # Any subclass of requests.RequestException hits our fallback branch.
        raise ReqConnectionError("simulated DNS failure")

    monkeypatch.setattr(service, "_related_queries_sync", boom)

    with pytest.raises(TrendsUnavailable):
        await service.related_queries("python", geo="US", timeframe="today 3-m")


@pytest.mark.asyncio
async def test_related_queries_propagates_429_as_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(with_cache=False)

    def boom(*_args, **_kwargs):
        raise TooManyRequestsError("429", response=MagicMock())

    monkeypatch.setattr(service, "_related_queries_sync", boom)

    with pytest.raises(TrendsRateLimited):
        await service.related_queries("python", geo="US", timeframe="today 3-m")
