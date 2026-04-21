"""Tests for :mod:`bot.services.cache`."""

from __future__ import annotations

import asyncio

import pytest

from bot.services.cache import TTLKeyCache, make_key


@pytest.mark.asyncio
async def test_get_set_roundtrip() -> None:
    cache = TTLKeyCache(maxsize=4, ttl=60)
    await cache.set("k", "v")
    assert await cache.get("k") == "v"


@pytest.mark.asyncio
async def test_stats_hits_and_misses() -> None:
    cache = TTLKeyCache(maxsize=4, ttl=60)
    assert await cache.get("absent") is None
    await cache.set("k", 1)
    assert await cache.get("k") == 1
    assert await cache.get("k") == 1
    stats = cache.stats()
    assert stats.hits == 2
    assert stats.misses == 1
    assert stats.size == 1


@pytest.mark.asyncio
async def test_invalidate() -> None:
    cache = TTLKeyCache(maxsize=4, ttl=60)
    await cache.set("k", "v")
    await cache.invalidate("k")
    assert await cache.get("k") is None


@pytest.mark.asyncio
async def test_ttl_expiry() -> None:
    cache = TTLKeyCache(maxsize=4, ttl=1)
    await cache.set("k", "v")
    assert await cache.get("k") == "v"
    await asyncio.sleep(1.1)
    assert await cache.get("k") is None


def test_make_key_stability() -> None:
    assert make_key("x", 1, ["b", "A"]) == make_key("X", 1, ["a", "B"])
