"""Snapshot append with 5-minute same-machine collapse.

Multiple machines' snapshots are always preserved. But when one machine
loops through the same Short twice within 5 minutes with essentially the
same numbers, we skip the second observation — otherwise the DB bloats
and velocity fits get dominated by redundant points.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import VideoMetricsSnapshot


COLLAPSE_WINDOW = timedelta(minutes=5)
COLLAPSE_VIEW_DELTA = 100


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _parse_uuid(value: Any):
    from uuid import UUID

    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _should_collapse(
    recent: VideoMetricsSnapshot | None, new_views: int
) -> bool:
    if recent is None:
        return False
    if recent.view_count is None:
        return False
    return abs(recent.view_count - new_views) < COLLAPSE_VIEW_DELTA


def append_snapshot(
    db: Session,
    video_id: int,
    obs: dict,
    *,
    machine_id: str | None,
    session_id: str | None,
) -> bool:
    """Insert one snapshot. Returns True if inserted, False if collapsed."""
    observed_at = _parse_dt(obs.get("observed_at")) or datetime.utcnow()
    view_count = int(obs.get("view_count") or 0)
    machine_uuid = _parse_uuid(machine_id)
    session_uuid = _parse_uuid(session_id)

    if machine_uuid is not None:
        cutoff = observed_at - COLLAPSE_WINDOW
        recent = db.execute(
            select(VideoMetricsSnapshot)
            .where(VideoMetricsSnapshot.video_id == video_id)
            .where(VideoMetricsSnapshot.source_machine_id == machine_uuid)
            .where(VideoMetricsSnapshot.captured_at > cutoff)
            .order_by(VideoMetricsSnapshot.captured_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if _should_collapse(recent, view_count):
            return False

    db.add(
        VideoMetricsSnapshot(
            video_id=video_id,
            captured_at=observed_at,
            view_count=view_count,
            like_count=obs.get("like_count"),
            comment_count=obs.get("comment_count"),
            source_machine_id=machine_uuid,
            source_session_id=session_uuid,
        )
    )
    return True
