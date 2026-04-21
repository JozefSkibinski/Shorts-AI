"""Async database abstraction.

SQLite is the default backend (via ``aiosqlite``). The :class:`Database`
interface only exposes ``execute`` / ``fetchone`` / ``fetchall`` / transaction
helpers, so swapping in a Postgres implementation later (e.g. ``asyncpg``) is
a matter of subclassing :class:`Database` and pointing ``build_database`` at
the new class when the URL starts with ``postgresql://``.

We keep the abstraction deliberately thin (just enough for our two repos)
rather than reaching for a full ORM — the write volume is low, the schema
is tiny, and this keeps dependencies minimal.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urlparse

import aiosqlite

log = logging.getLogger(__name__)


class Database(ABC):
    """Minimal async DB interface used by the repositories."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def execute(
        self, query: str, params: Sequence[Any] | None = None
    ) -> None: ...

    @abstractmethod
    async def executemany(
        self, query: str, param_seq: Iterable[Sequence[Any]]
    ) -> None: ...

    @abstractmethod
    async def fetchone(
        self, query: str, params: Sequence[Any] | None = None
    ) -> Optional[dict[str, Any]]: ...

    @abstractmethod
    async def fetchall(
        self, query: str, params: Sequence[Any] | None = None
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def init_schema(self) -> None: ...


class SqliteDatabase(Database):
    """aiosqlite-backed :class:`Database`."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        parent = Path(self._path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        # Enable foreign keys + WAL for better concurrency behaviour.
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.commit()
        log.info("SQLite database ready at %s", self._path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called first")
        return self._conn

    async def execute(self, query, params=None):
        conn = self._require()
        await conn.execute(query, params or ())
        await conn.commit()

    async def executemany(self, query, param_seq):
        conn = self._require()
        await conn.executemany(query, list(param_seq))
        await conn.commit()

    async def fetchone(self, query, params=None):
        conn = self._require()
        async with conn.execute(query, params or ()) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def fetchall(self, query, params=None):
        conn = self._require()
        async with conn.execute(query, params or ()) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def init_schema(self) -> None:
        conn = self._require()
        # Each CREATE TABLE is IF NOT EXISTS so init is idempotent (doubles as
        # the "migration" mechanism for a schema this size; move to Alembic or
        # SQL migration files once it grows).
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id           INTEGER PRIMARY KEY,
                default_geo        TEXT    NOT NULL DEFAULT '',
                default_timeframe  TEXT    NOT NULL DEFAULT 'today 3-m',
                digest_channel_id  INTEGER,
                digest_seed        TEXT    NOT NULL DEFAULT 'youtube',
                updated_at         TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS trend_snapshots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                source       TEXT    NOT NULL,  -- 'trends' | 'youtube_api'
                kind         TEXT    NOT NULL,  -- 'related' | 'top' | 'compare'
                keyword      TEXT    NOT NULL,  -- canonical keyword or comma-joined
                geo          TEXT    NOT NULL,
                timeframe    TEXT    NOT NULL,
                payload      TEXT    NOT NULL   -- JSON
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
                ON trend_snapshots (keyword, geo, timeframe, captured_at DESC);

            CREATE INDEX IF NOT EXISTS idx_snapshots_kind_time
                ON trend_snapshots (kind, captured_at DESC);
            """
        )
        await conn.commit()
        log.info("Database schema initialised")


def build_database(database_url: str) -> Database:
    """Factory — pick a backend based on the URL scheme.

    Currently only ``sqlite://`` URLs are supported. Adding Postgres later
    would be a new ``PostgresDatabase`` class selected here on scheme match.
    """
    parsed = urlparse(database_url)
    scheme = parsed.scheme.lower()

    if scheme in ("sqlite", ""):
        # URL shapes accepted:
        #   sqlite:///relative/path.db
        #   sqlite:////absolute/path.db
        #   ./relative/path.db
        path = parsed.path or database_url
        path = path.lstrip("/") if parsed.netloc == "" and path.startswith("/") and not os.path.isabs(path) else path
        if not path:
            path = "data/shorts_ai.db"
        return SqliteDatabase(path)

    raise ValueError(
        f"Unsupported DATABASE_URL scheme: {scheme!r}. "
        "sqlite:// is the only built-in; add a backend class for others."
    )
