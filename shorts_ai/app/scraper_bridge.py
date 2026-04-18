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

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Video, VideoAnalysis, VideoMetricsSnapshot
from app.db.session import session_scope


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


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
            raw=payload,
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
    stmt = (
        pg_insert(Video)
        .values(
            source_video_id=row.video_id,
            url=row.url,
            author_handle=row.channel or None,
            title=row.title or None,
            description=row.description or None,
            hashtags=row.hashtags or None,
            duration_seconds=row.duration_seconds,
            raw_scraper_payload=row.raw,
        )
        .on_conflict_do_update(
            index_elements=["source_video_id"],
            set_={
                "url": row.url,
                "author_handle": row.channel or None,
                "title": row.title or None,
                "description": row.description or None,
                "hashtags": row.hashtags or None,
                "duration_seconds": row.duration_seconds,
                "raw_scraper_payload": row.raw,
            },
        )
        .returning(Video.id)
    )
    return db.execute(stmt).scalar_one()


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
    rows_seen: int = 0
    rows_ingested: int = 0
    enrich_enqueued: int = 0
    last_offset: int = 0

    def merge(self, other: "IngestStats") -> "IngestStats":
        return IngestStats(
            rows_seen=self.rows_seen + other.rows_seen,
            rows_ingested=self.rows_ingested + other.rows_ingested,
            enrich_enqueued=self.enrich_enqueued + other.enrich_enqueued,
            last_offset=other.last_offset or self.last_offset,
        )


def ingest_rows(
    db: Session, rows: Iterable[ScraperRow], enqueue_fn=None
) -> IngestStats:
    """Insert rows + snapshots, schedule enrichment for new videos."""
    stats = IngestStats()
    for row in rows:
        stats.rows_seen += 1
        try:
            video_pk = upsert_video(db, row)
            append_snapshot(db, video_pk, row)
            stats.rows_ingested += 1
            if enqueue_fn and needs_enrichment(db, video_pk):
                enqueue_fn(video_pk)
                stats.enrich_enqueued += 1
        except Exception as exc:
            log.exception("Failed to ingest %s: %s", row.video_id, exc)
    return stats


def _default_enqueue(video_pk: int) -> None:
    """Enqueue Celery task without importing the worker eagerly."""
    try:
        from workers.celery_app import enrich_video
    except Exception as exc:
        log.warning("Celery import failed (%s); skipping enqueue.", exc)
        return
    enrich_video.delay(video_pk)


def run_once(path: Path, start_offset: int, enqueue_fn=None) -> IngestStats:
    rows: list[ScraperRow] = []
    new_offset = start_offset
    for offset, payload in iter_jsonl(path, start_offset):
        new_offset = offset
        row = ScraperRow.from_dict(payload)
        if row:
            rows.append(row)
    if not rows:
        return IngestStats(last_offset=new_offset)
    with session_scope() as db:
        stats = ingest_rows(db, rows, enqueue_fn=enqueue_fn)
    stats.last_offset = new_offset
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
    path = args.path or settings.scraper_output_path
    enqueue = None if args.no_enqueue else _default_enqueue

    if not (args.backfill or args.watch):
        args.backfill = True

    offset = 0
    stats = run_once(path, offset, enqueue_fn=enqueue)
    log.info(
        "Backfill: %d rows seen, %d ingested, %d enrichment tasks queued.",
        stats.rows_seen,
        stats.rows_ingested,
        stats.enrich_enqueued,
    )
    offset = stats.last_offset

    if args.watch:
        log.info("Watching %s every %ds", path, settings.bridge_poll_seconds)
        try:
            while True:
                time.sleep(settings.bridge_poll_seconds)
                tick = run_once(path, offset, enqueue_fn=enqueue)
                offset = tick.last_offset
                if tick.rows_ingested:
                    log.info(
                        "Tick: +%d rows, +%d enqueued.",
                        tick.rows_ingested,
                        tick.enrich_enqueued,
                    )
        except KeyboardInterrupt:
            log.info("Stopping watcher.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
