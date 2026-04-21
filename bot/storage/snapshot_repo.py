"""Repository for historical trend snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bot.storage.database import Database


@dataclass(frozen=True)
class TrendSnapshot:
    """A point-in-time capture of a trend query result."""

    id: Optional[int]
    captured_at: datetime
    source: str         # 'trends' | 'youtube_api'
    kind: str           # 'related' | 'top' | 'compare'
    keyword: str        # canonical key — single keyword or comma-joined list
    geo: str
    timeframe: str
    payload: dict[str, Any] = field(default_factory=dict)


class SnapshotRepo:
    """Historical snapshots used for week-over-week diffs and audit."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(self, snap: TrendSnapshot) -> int:
        await self._db.execute(
            """
            INSERT INTO trend_snapshots
                (captured_at, source, kind, keyword, geo, timeframe, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snap.captured_at.astimezone(timezone.utc)
                    .strftime("%Y-%m-%d %H:%M:%S"),
                snap.source,
                snap.kind,
                snap.keyword,
                snap.geo,
                snap.timeframe,
                json.dumps(snap.payload, default=str),
            ),
        )
        # SQLite-specific: last insert rowid. For Postgres we'd use RETURNING.
        row = await self._db.fetchone("SELECT last_insert_rowid() AS id")
        return int(row["id"]) if row else 0

    async def latest(
        self, *, keyword: str, geo: str, timeframe: str, kind: str
    ) -> Optional[TrendSnapshot]:
        row = await self._db.fetchone(
            """
            SELECT id, captured_at, source, kind, keyword, geo, timeframe, payload
            FROM trend_snapshots
            WHERE keyword = ? AND geo = ? AND timeframe = ? AND kind = ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (keyword, geo, timeframe, kind),
        )
        return _row_to_snapshot(row)

    async def closest_before(
        self,
        *,
        keyword: str,
        geo: str,
        timeframe: str,
        kind: str,
        cutoff: datetime,
    ) -> Optional[TrendSnapshot]:
        """Return the most recent snapshot strictly older than ``cutoff``."""
        row = await self._db.fetchone(
            """
            SELECT id, captured_at, source, kind, keyword, geo, timeframe, payload
            FROM trend_snapshots
            WHERE keyword = ? AND geo = ? AND timeframe = ? AND kind = ?
              AND captured_at <= ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (
                keyword, geo, timeframe, kind,
                cutoff.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        return _row_to_snapshot(row)

    async def week_ago(
        self, *, keyword: str, geo: str, timeframe: str, kind: str
    ) -> Optional[TrendSnapshot]:
        """Convenience: closest snapshot at least 6 days old.

        We use 6 days instead of exactly 7 to forgive clock drift and let a
        daily digest run line up with the previous week's digest.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=6)
        return await self.closest_before(
            keyword=keyword, geo=geo, timeframe=timeframe, kind=kind, cutoff=cutoff
        )

    async def prune_older_than(self, days: int) -> int:
        """Delete snapshots older than ``days`` (simple retention policy)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        before = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM trend_snapshots WHERE captured_at < ?",
            (cutoff_str,),
        )
        await self._db.execute(
            "DELETE FROM trend_snapshots WHERE captured_at < ?",
            (cutoff_str,),
        )
        return int(before["n"]) if before else 0


def _row_to_snapshot(row: Optional[dict[str, Any]]) -> Optional[TrendSnapshot]:
    if row is None:
        return None
    captured = row["captured_at"]
    if isinstance(captured, str):
        captured_dt = datetime.strptime(captured, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    else:
        captured_dt = captured
    return TrendSnapshot(
        id=row["id"],
        captured_at=captured_dt,
        source=row["source"],
        kind=row["kind"],
        keyword=row["keyword"],
        geo=row["geo"],
        timeframe=row["timeframe"],
        payload=json.loads(row["payload"]) if row["payload"] else {},
    )


def diff_top_queries(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Classify queries across two snapshots into 'new', 'dropped', 'common'."""
    cur_set = {str(r.get("query", "")).lower() for r in current if r.get("query")}
    prev_set = {str(r.get("query", "")).lower() for r in previous if r.get("query")}
    return {
        "new": sorted(cur_set - prev_set),
        "dropped": sorted(prev_set - cur_set),
        "common": sorted(cur_set & prev_set),
    }
