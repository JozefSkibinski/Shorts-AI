"""Central batch ingestor.

Pulls gzipped JSONL batches produced by distributed scraper machines,
dedups them into canonical ``videos`` rows, appends per-observation
snapshots (tagged by machine + session), upserts ScraperMachine /
ScraperSession rows from the batch header, and enqueues enrichment for
videos seen for the first time.

Each batch lands in one of three terminal states in ``raw_batches``:

    pending      — the row exists but the bytes haven't been processed yet
    ingested     — committed cleanly and moved to the archive prefix
    failed       — a transient error; may be retried
    quarantined  — the batch itself was malformed (bad header / not JSON)

Run modes (mirror the bridge):

    python -m app.ingestor.main --backfill
    python -m app.ingestor.main --watch
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    RawBatch,
    ScraperMachine,
    ScraperSession,
    Video,
    VideoAnalysis,
)
from app.db.session import session_scope
from app.ingestor.bucket import Bucket, LocalBucket
from app.ingestor.dedup import upsert_video
from app.ingestor.metrics_merge import append_snapshot
from app.ingestor.reader import BatchValidationError, ParsedBatch, parse_batch


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Upsert helpers for header/footer
# ---------------------------------------------------------------------------


def _parse_uuid(value: Any) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def upsert_machine(db: Session, header: dict) -> UUID | None:
    mid = _parse_uuid(header.get("machine_id"))
    if mid is None:
        return None
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(ScraperMachine)
        .values(
            id=mid,
            hostname=header.get("hostname"),
            last_seen_at=now,
            total_batches=1,
            scraper_version=header.get("scraper_version"),
        )
        .on_conflict_do_update(
            index_elements=["id"],
            set_={
                "hostname": header.get("hostname"),
                "last_seen_at": now,
                "total_batches": ScraperMachine.total_batches + 1,
                "scraper_version": header.get("scraper_version"),
            },
        )
    )
    db.execute(stmt)
    return mid


def upsert_session_from_header(
    db: Session, header: dict, footer: dict | None, machine_id: UUID | None
) -> UUID | None:
    sid = _parse_uuid(header.get("session_id"))
    if sid is None:
        return None
    created = _parse_dt(header.get("session_started_at")) or datetime.now(timezone.utc)
    closed = _parse_dt((footer or {}).get("closed_at"))
    ads = int((footer or {}).get("ads_filtered") or 0)
    viewport = header.get("viewport") or ""
    ua = header.get("user_agent") or ""

    values = {
        "id": sid,
        "created_at": created,
        "closed_at": closed,
        "user_agent": ua or None,
        "viewport": viewport or None,
        "ads_filtered": ads,
        "machine_id": machine_id,
    }
    stmt = (
        pg_insert(ScraperSession)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["id"],
            set_={
                "closed_at": closed,
                "user_agent": ua or None,
                "viewport": viewport or None,
                "ads_filtered": ads,
                "machine_id": machine_id,
            },
        )
    )
    db.execute(stmt)
    return sid


# ---------------------------------------------------------------------------
# Single batch ingestion
# ---------------------------------------------------------------------------


def needs_enrichment(db: Session, video_id: int) -> bool:
    return (
        db.execute(
            select(VideoAnalysis.video_id).where(VideoAnalysis.video_id == video_id)
        ).first()
        is None
    )


def _default_enqueue(video_pk: int) -> None:
    try:
        from workers.celery_app import enrich_video
    except Exception as exc:
        log.warning("Celery import failed (%s); skipping enqueue.", exc)
        return
    enrich_video.delay(video_pk)


def _record_raw_batch(
    db: Session,
    key: str,
    batch: ParsedBatch | None,
    status: str,
    error: str | None,
) -> None:
    values = {
        "s3_key": key,
        "machine_id": _parse_uuid(batch.machine_id) if batch else None,
        "session_id": _parse_uuid(batch.session_id) if batch else None,
        "uploaded_at": datetime.now(timezone.utc),
        "ingested_at": datetime.now(timezone.utc) if status == "ingested" else None,
        "records": len(batch.observations) if batch else None,
        "status": status,
        "error": error,
    }
    stmt = (
        pg_insert(RawBatch)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["s3_key"],
            set_={
                "ingested_at": values["ingested_at"],
                "records": values["records"],
                "status": values["status"],
                "error": values["error"],
            },
        )
    )
    db.execute(stmt)


def ingest_batch(
    bucket: Bucket,
    key: str,
    *,
    enqueue_fn=None,
) -> dict:
    """Parse + commit one batch. Returns a stats dict."""
    stats = {
        "key": key,
        "records": 0,
        "new_videos": 0,
        "snapshots_inserted": 0,
        "snapshots_collapsed": 0,
        "status": "pending",
    }

    try:
        with bucket.open(key) as fh:
            batch = parse_batch(fh)
    except BatchValidationError as exc:
        log.error("Quarantining %s: %s", key, exc)
        with session_scope() as db:
            _record_raw_batch(db, key, None, "quarantined", str(exc))
        stats["status"] = "quarantined"
        return stats
    except Exception as exc:
        log.exception("Failed to read batch %s: %s", key, exc)
        with session_scope() as db:
            _record_raw_batch(db, key, None, "failed", str(exc))
        stats["status"] = "failed"
        return stats

    stats["records"] = len(batch.observations)

    try:
        with session_scope() as db:
            machine_uuid = upsert_machine(db, batch.header)
            session_uuid = upsert_session_from_header(
                db, batch.header, batch.footer, machine_uuid
            )
            machine_id_str = str(machine_uuid) if machine_uuid else None
            session_id_str = str(session_uuid) if session_uuid else None

            for obs in batch.observations:
                video_id, is_new = upsert_video(db, obs, session_id_str)
                inserted = append_snapshot(
                    db,
                    video_id,
                    obs,
                    machine_id=machine_id_str,
                    session_id=session_id_str,
                )
                if inserted:
                    stats["snapshots_inserted"] += 1
                else:
                    stats["snapshots_collapsed"] += 1
                if is_new:
                    stats["new_videos"] += 1
                    if enqueue_fn:
                        enqueue_fn(video_id)

            _record_raw_batch(db, key, batch, "ingested", None)
    except Exception as exc:
        log.exception("Ingest failed for %s: %s", key, exc)
        with session_scope() as db:
            _record_raw_batch(db, key, batch, "failed", str(exc))
        stats["status"] = "failed"
        return stats

    # Only archive after the DB transaction committed successfully.
    try:
        bucket.move(key, bucket.archive_key_for(key))
    except Exception as exc:
        log.warning("archive move failed for %s: %s", key, exc)

    stats["status"] = "ingested"
    return stats


def ingest_bucket_once(bucket: Bucket, *, enqueue_fn=None, limit: int | None = None) -> list[dict]:
    """Ingest all currently-visible batch keys. Returns per-batch stats."""
    results: list[dict] = []
    for i, key in enumerate(bucket.list_keys()):
        if limit is not None and i >= limit:
            break
        results.append(ingest_batch(bucket, key, enqueue_fn=enqueue_fn))
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest distributed scraper batches.")
    p.add_argument(
        "--root",
        type=Path,
        default=Path("../output/batches"),
        help="Local staging root (when not using S3). Default: ../output/batches.",
    )
    p.add_argument("--s3-bucket", default=None, help="If set, read from this S3 bucket.")
    p.add_argument("--s3-prefix", default="", help="Optional key prefix inside --s3-bucket.")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--watch", action="store_true")
    p.add_argument(
        "--no-enqueue",
        action="store_true",
        help="Skip Celery enqueue. Useful for dry-runs.",
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

    if args.s3_bucket:
        from app.ingestor.bucket import S3Bucket

        bucket: Bucket = S3Bucket(args.s3_bucket, prefix=args.s3_prefix)
    else:
        bucket = LocalBucket(args.root.resolve())

    enqueue = None if args.no_enqueue else _default_enqueue

    if not (args.backfill or args.watch):
        args.backfill = True

    batch_stats = ingest_bucket_once(bucket, enqueue_fn=enqueue)
    _summarize(batch_stats)

    if args.watch:
        log.info("Watching bucket every %ds", settings.bridge_poll_seconds)
        try:
            while True:
                time.sleep(settings.bridge_poll_seconds)
                tick = ingest_bucket_once(bucket, enqueue_fn=enqueue)
                if tick:
                    _summarize(tick)
        except KeyboardInterrupt:
            log.info("Stopping watcher.")
    return 0


def _summarize(batch_stats: list[dict]) -> None:
    if not batch_stats:
        return
    by_status: dict[str, int] = {}
    total_records = 0
    new_videos = 0
    for s in batch_stats:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
        total_records += s["records"]
        new_videos += s["new_videos"]
    log.info(
        "Ingested %d batches (%s): %d observations, %d new videos.",
        len(batch_stats),
        ", ".join(f"{k}={v}" for k, v in by_status.items()),
        total_records,
        new_videos,
    )


if __name__ == "__main__":
    raise SystemExit(main())
