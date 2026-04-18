"""Exact dedup: one Video row per source_video_id, regardless of how many
machines observed it. ``first_seen_at`` / ``first_seen_by_session`` are set
only on first insert; ``last_seen_at`` is refreshed on every observation.
Title/description/hashtags are overwritten on each observation because
creators occasionally edit them and the latest is usually the best version
for retrieval.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Video


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def upsert_video(
    db: Session, obs: dict, session_id: str | None
) -> tuple[int, bool]:
    """Insert or update the Video row. Returns (video_id, is_new)."""
    observed_at = _parse_dt(obs.get("observed_at"))
    published_at = _parse_dt(obs.get("published_at"))

    # Columns we always want to refresh on every observation.
    refresh = {
        "url": obs.get("url"),
        "author_handle": obs.get("author_handle"),
        "author_id": obs.get("author_id"),
        "title": obs.get("title"),
        "description": obs.get("description"),
        "hashtags": obs.get("hashtags") or None,
        "duration_seconds": obs.get("duration_seconds"),
        "published_at": published_at,
        "has_paid_promotion_disclosure": bool(
            obs.get("has_paid_promotion_disclosure", False)
        ),
        "last_seen_at": observed_at,
    }
    # Columns we only want to set on first insert.
    first_only = {
        "source_video_id": obs["source_video_id"],
        "first_seen_at": observed_at,
        "first_seen_by_session": session_id,
    }

    stmt = (
        pg_insert(Video)
        .values(**first_only, **refresh)
        .on_conflict_do_update(
            index_elements=["source_video_id"],
            set_=refresh,
        )
        .returning(Video.id, Video.first_seen_at)
    )
    row = db.execute(stmt).one()
    video_id = int(row.id)
    is_new = observed_at is not None and row.first_seen_at == observed_at
    return video_id, is_new
