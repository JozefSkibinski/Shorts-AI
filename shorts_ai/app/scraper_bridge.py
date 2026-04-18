"""Adapter from the existing scraper's shorts.jsonl into the Phase 1 DB.

Run modes:

  python -m app.scraper_bridge --backfill
      Read every line of shorts.jsonl, upsert videos, append a snapshot,
      enqueue enrichment for any video without a VideoAnalysis row.

  python -m app.scraper_bridge --watch
      Same as --backfill, then poll for new lines forever (default
      every BRIDGE_POLL_SECONDS seconds).

The bridge is intentionally idempotent. shorts.jsonl is append-only;
each line is keyed by source_video_id (UNIQUE), so re-ingesting the
same line is a no-op for the videos table and adds a fresh metrics
snapshot (so the velocity calculator has something to work with).
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    ScraperSession,
    Video,
    VideoAdVerdict,
    VideoAnalysis,
    VideoMetricsSnapshot,
)
from app.db.session import session_scope


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_uuid(value) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class ScraperRow:
    """Validated shape of one shorts.jsonl line."""

    video_id: str
    url: str
    channel: str
    title: str
    description: str
    hashtags: list[str]
    keywords: list[str]
    view_count: int
    duration_seconds: float | None
    transcript: str | None
    hook: str | None
    metadata_source: str
    transcript_source: str
    has_paid_promotion_disclosure: bool
    session_id: UUID | None
    raw: dict

    @classmethod
    def from_dict(cls, payload: dict) -> "ScraperRow | None":
        vid = payload.get("video_id")
        url = payload.get("url")
        if not vid or not url:
            return None
        try:
            views = int(payload.get("view_count") or 0)
        except (TypeError, ValueError):
            views = 0
        duration = payload.get("duration_seconds")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        return cls(
            video_id=str(vid),
            url=str(url),
            channel=str(payload.get("channel") or ""),
            title=str(payload.get("title") or ""),
            description=str(payload.get("description") or ""),
            hashtags=list(payload.get("hashtags") or []),
            keywords=list(payload.get("keywords") or []),
            view_count=views,
            duration_seconds=duration,
            transcript=payload.get("transcript"),
            hook=payload.get("hook"),
            metadata_source=str(payload.get("metadata_source") or ""),
            transcript_source=str(payload.get("transcript_source") or "none"),
            has_paid_promotion_disclosure=bool(
                payload.get("has_paid_promotion_disclosure", False)
            ),
            session_id=_parse_uuid(payload.get("session_id")),
            raw=payload,
        )


@dataclass
class AdDropRow:
    """Validated shape of one ads_filtered.jsonl line."""

    video_id: str
    url: str
    session_id: UUID | None
    reasons: list[str]
    raw: dict

    @classmethod
    def from_dict(cls, payload: dict) -> "AdDropRow | None":
        vid = payload.get("video_id")
        if not vid:
            return None
        return cls(
            video_id=str(vid),
            url=str(payload.get("url") or f"https://www.youtube.com/shorts/{vid}"),
            session_id=_parse_uuid(payload.get("session_id")),
            reasons=list(payload.get("reasons") or []),
            raw=payload,
        )


@dataclass
class SessionRow:
    """Validated shape of one sessions.jsonl line."""

    session_id: UUID
    created_at: datetime
    closed_at: datetime | None
    user_agent: str
    viewport: str
    videos_scraped: int
    ads_filtered: int
    unique_channels_seen: int
    channels_distribution: dict

    @classmethod
    def from_dict(cls, payload: dict) -> "SessionRow | None":
        sid = _parse_uuid(payload.get("session_id"))
        if sid is None:
            return None
        try:
            created = datetime.fromisoformat(str(payload.get("created_at")))
        except (TypeError, ValueError):
            return None
        closed_raw = payload.get("closed_at")
        closed: datetime | None
        try:
            closed = datetime.fromisoformat(str(closed_raw)) if closed_raw else None
        except (TypeError, ValueError):
            closed = None
        return cls(
            session_id=sid,
            created_at=created,
            closed_at=closed,
            user_agent=str(payload.get("user_agent") or ""),
            viewport=str(payload.get("viewport") or ""),
            videos_scraped=int(payload.get("videos_scraped") or 0),
            ads_filtered=int(payload.get("ads_filtered") or 0),
            unique_channels_seen=int(payload.get("unique_channels_seen") or 0),
            channels_distribution=dict(payload.get("channels_distribution") or {}),
        )


def iter_jsonl(path: Path, start_offset: int = 0) -> Iterator[tuple[int, dict]]:
    """Yield (next_offset, payload) for every JSON line starting at start_offset."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        f.seek(start_offset)
        while True:
            line_start = f.tell()
            line = f.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Skipping malformed jsonl line at offset %d", line_start)
                continue
            yield (f.tell(), payload)


# ---------------------------------------------------------------------------
# Upsert + snapshot
# ---------------------------------------------------------------------------


def upsert_video(db: Session, row: ScraperRow) -> int:
    """Insert or update the videos row. Returns the videos.id."""
    values = {
        "source_video_id": row.video_id,
        "url": row.url,
        "author_handle": row.channel or None,
        "title": row.title or None,
        "description": row.description or None,
        "hashtags": row.hashtags or None,
        "duration_seconds": row.duration_seconds,
        "raw_scraper_payload": row.raw,
        "is_ad": False,
        "has_paid_promotion_disclosure": row.has_paid_promotion_disclosure,
        "session_id": row.session_id,
    }
    stmt = (
        pg_insert(Video)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["source_video_id"],
            set_={k: v for k, v in values.items() if k != "source_video_id"},
        )
        .returning(Video.id)
    )
    return db.execute(stmt).scalar_one()


def upsert_ad_drop(db: Session, drop: AdDropRow) -> int:
    """Record a dropped ad as a videos row with is_ad=true + a verdict row."""
    values = {
        "source_video_id": drop.video_id,
        "url": drop.url,
        "is_ad": True,
        "has_paid_promotion_disclosure": False,
        "session_id": drop.session_id,
        "raw_scraper_payload": drop.raw,
    }
    stmt = (
        pg_insert(Video)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["source_video_id"],
            set_={
                "is_ad": True,
                "session_id": drop.session_id,
                "raw_scraper_payload": drop.raw,
            },
        )
        .returning(Video.id)
    )
    video_pk = db.execute(stmt).scalar_one()

    verdict_stmt = (
        pg_insert(VideoAdVerdict)
        .values(video_id=video_pk, is_ad=True, reasons=drop.reasons or None)
        .on_conflict_do_update(
            index_elements=["video_id"],
            set_={"is_ad": True, "reasons": drop.reasons or None},
        )
    )
    db.execute(verdict_stmt)
    return video_pk


def upsert_session(db: Session, row: SessionRow) -> None:
    values = {
        "id": row.session_id,
        "created_at": row.created_at,
        "closed_at": row.closed_at,
        "user_agent": row.user_agent or None,
        "viewport": row.viewport or None,
        "videos_scraped": row.videos_scraped,
        "ads_filtered": row.ads_filtered,
        "unique_channels_seen": row.unique_channels_seen,
        "channels_distribution": row.channels_distribution or None,
    }
    stmt = (
        pg_insert(ScraperSession)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["id"],
            set_={k: v for k, v in values.items() if k != "id"},
        )
    )
    db.execute(stmt)


def append_snapshot(db: Session, video_pk: int, row: ScraperRow) -> None:
    db.add(
        VideoMetricsSnapshot(
            video_id=video_pk,
            view_count=row.view_count,
        )
    )


def needs_enrichment(db: Session, video_pk: int) -> bool:
    return (
        db.execute(
            select(VideoAnalysis.video_id).where(VideoAnalysis.video_id == video_pk)
        ).first()
        is None
    )


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@dataclass
class IngestStats:
    videos_seen: int = 0
    videos_ingested: int = 0
    enrich_enqueued: int = 0
    ads_ingested: int = 0
    sessions_ingested: int = 0
    video_offset: int = 0
    ad_offset: int = 0
    session_offset: int = 0


def ingest_video_rows(
    db: Session, rows: Iterable[ScraperRow], enqueue_fn=None
) -> tuple[int, int, int]:
    """Returns (seen, ingested, enqueued)."""
    seen = ingested = enqueued = 0
    for row in rows:
        seen += 1
        try:
            video_pk = upsert_video(db, row)
            append_snapshot(db, video_pk, row)
            ingested += 1
            if enqueue_fn and needs_enrichment(db, video_pk):
                enqueue_fn(video_pk)
                enqueued += 1
        except Exception as exc:
            log.exception("Failed to ingest %s: %s", row.video_id, exc)
    return seen, ingested, enqueued


def ingest_ad_rows(db: Session, rows: Iterable[AdDropRow]) -> int:
    ingested = 0
    for row in rows:
        try:
            upsert_ad_drop(db, row)
            ingested += 1
        except Exception as exc:
            log.exception("Failed to ingest ad drop %s: %s", row.video_id, exc)
    return ingested


def ingest_session_rows(db: Session, rows: Iterable[SessionRow]) -> int:
    ingested = 0
    for row in rows:
        try:
            upsert_session(db, row)
            ingested += 1
        except Exception as exc:
            log.exception("Failed to ingest session %s: %s", row.session_id, exc)
    return ingested


def _default_enqueue(video_pk: int) -> None:
    """Enqueue Celery task without importing the worker eagerly."""
    try:
        from workers.celery_app import enrich_video
    except Exception as exc:
        log.warning("Celery import failed (%s); skipping enqueue.", exc)
        return
    enrich_video.delay(video_pk)


def _collect(path: Path, start_offset: int, cls) -> tuple[list, int]:
    rows = []
    new_offset = start_offset
    for offset, payload in iter_jsonl(path, start_offset):
        new_offset = offset
        parsed = cls.from_dict(payload)
        if parsed:
            rows.append(parsed)
    return rows, new_offset


def run_once(
    shorts_path: Path,
    ads_path: Path | None,
    sessions_path: Path | None,
    *,
    shorts_offset: int = 0,
    ads_offset: int = 0,
    sessions_offset: int = 0,
    enqueue_fn=None,
) -> IngestStats:
    """Ingest every sibling file in one transaction.

    Order is important: sessions first (videos.session_id references them),
    then videos + ads. All three share a single session_scope so a failure
    anywhere rolls the whole tick back.
    """
    stats = IngestStats(
        video_offset=shorts_offset,
        ad_offset=ads_offset,
        session_offset=sessions_offset,
    )

    video_rows, stats.video_offset = _collect(shorts_path, shorts_offset, ScraperRow)
    ad_rows: list[AdDropRow] = []
    session_rows: list[SessionRow] = []
    if ads_path is not None:
        ad_rows, stats.ad_offset = _collect(ads_path, ads_offset, AdDropRow)
    if sessions_path is not None:
        session_rows, stats.session_offset = _collect(
            sessions_path, sessions_offset, SessionRow
        )

    if not (video_rows or ad_rows or session_rows):
        return stats

    with session_scope() as db:
        stats.sessions_ingested = ingest_session_rows(db, session_rows)
        seen, ingested, enqueued = ingest_video_rows(
            db, video_rows, enqueue_fn=enqueue_fn
        )
        stats.videos_seen = seen
        stats.videos_ingested = ingested
        stats.enrich_enqueued = enqueued
        stats.ads_ingested = ingest_ad_rows(db, ad_rows)

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bridge shorts.jsonl into the DB.")
    p.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Path to shorts.jsonl (default: from settings.scraper_output_path).",
    )
    p.add_argument(
        "--ads-path",
        type=Path,
        default=None,
        help="Path to ads_filtered.jsonl (default: sibling of shorts.jsonl).",
    )
    p.add_argument(
        "--sessions-path",
        type=Path,
        default=None,
        help="Path to sessions.jsonl (default: sibling of shorts.jsonl).",
    )
    p.add_argument("--backfill", action="store_true", help="Process the file once.")
    p.add_argument(
        "--watch",
        action="store_true",
        help="Process once, then poll for new lines forever.",
    )
    p.add_argument(
        "--no-enqueue",
        action="store_true",
        help="Skip enqueuing Celery enrichment tasks (useful for tests).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    settings = get_settings()
    shorts_path = args.path or settings.scraper_output_path
    ads_path = args.ads_path or shorts_path.parent / "ads_filtered.jsonl"
    sessions_path = args.sessions_path or shorts_path.parent / "sessions.jsonl"
    enqueue = None if args.no_enqueue else _default_enqueue

    if not (args.backfill or args.watch):
        args.backfill = True

    offsets = {"shorts": 0, "ads": 0, "sessions": 0}

    def _tick() -> IngestStats:
        stats = run_once(
            shorts_path,
            ads_path,
            sessions_path,
            shorts_offset=offsets["shorts"],
            ads_offset=offsets["ads"],
            sessions_offset=offsets["sessions"],
            enqueue_fn=enqueue,
        )
        offsets["shorts"] = stats.video_offset
        offsets["ads"] = stats.ad_offset
        offsets["sessions"] = stats.session_offset
        return stats

    stats = _tick()
    log.info(
        "Backfill: videos=%d (ingested %d, enqueued %d), ads=%d, sessions=%d.",
        stats.videos_seen,
        stats.videos_ingested,
        stats.enrich_enqueued,
        stats.ads_ingested,
        stats.sessions_ingested,
    )

    if args.watch:
        log.info(
            "Watching shorts=%s ads=%s sessions=%s every %ds",
            shorts_path,
            ads_path,
            sessions_path,
            settings.bridge_poll_seconds,
        )
        try:
            while True:
                time.sleep(settings.bridge_poll_seconds)
                tick = _tick()
                if tick.videos_ingested or tick.ads_ingested or tick.sessions_ingested:
                    log.info(
                        "Tick: +%d videos, +%d ads, +%d sessions (%d enqueued).",
                        tick.videos_ingested,
                        tick.ads_ingested,
                        tick.sessions_ingested,
                        tick.enrich_enqueued,
                    )
        except KeyboardInterrupt:
            log.info("Stopping watcher.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
