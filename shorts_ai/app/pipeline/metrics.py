"""Velocity / decile rank computation from VideoMetricsSnapshot rows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Video, VideoAnalysis, VideoMetricsSnapshot


log = logging.getLogger(__name__)


@dataclass
class Velocity:
    v24: float | None  # views / hour over the first 24h after first snapshot
    v72: float | None  # views / hour over the first 72h
    peak: float | None  # max delta-views / delta-hours between any two snapshots
    niche_percentile: float | None  # 0..1 within the same niche by peak velocity


def _hours_between(later: datetime, earlier: datetime) -> float:
    return max(0.0, (later - earlier).total_seconds() / 3600.0)


def _window_velocity(snapshots: Sequence[VideoMetricsSnapshot], hours: float) -> float | None:
    if len(snapshots) < 2:
        return None
    base = snapshots[0]
    cutoff = base.captured_at + timedelta(hours=hours)
    in_window = [s for s in snapshots if s.captured_at <= cutoff]
    if len(in_window) < 2:
        return None
    last = in_window[-1]
    elapsed = _hours_between(last.captured_at, base.captured_at)
    if elapsed <= 0:
        return None
    delta = (last.view_count or 0) - (base.view_count or 0)
    return max(0.0, delta / elapsed)


def _peak_velocity(snapshots: Sequence[VideoMetricsSnapshot]) -> float | None:
    if len(snapshots) < 2:
        return None
    best = 0.0
    for prev, curr in zip(snapshots, snapshots[1:]):
        elapsed = _hours_between(curr.captured_at, prev.captured_at)
        if elapsed <= 0:
            continue
        delta = (curr.view_count or 0) - (prev.view_count or 0)
        if delta <= 0:
            continue
        rate = delta / elapsed
        if rate > best:
            best = rate
    return best or None


def compute(db: Session, video_id: int) -> Velocity:
    """Compute v24 / v72 / peak / niche_percentile for ``video_id``."""
    snapshots = (
        db.execute(
            select(VideoMetricsSnapshot)
            .where(VideoMetricsSnapshot.video_id == video_id)
            .order_by(VideoMetricsSnapshot.captured_at)
        )
        .scalars()
        .all()
    )
    v24 = _window_velocity(snapshots, 24.0)
    v72 = _window_velocity(snapshots, 72.0)
    peak = _peak_velocity(snapshots)

    pct = _niche_percentile(db, video_id, peak)
    return Velocity(v24=v24, v72=v72, peak=peak, niche_percentile=pct)


def _niche_percentile(db: Session, video_id: int, peak: float | None) -> float | None:
    """Where this video's peak velocity sits within its niche, 0..1."""
    if peak is None:
        return None
    video = db.get(Video, video_id)
    if video is None or video.niche_id is None:
        return None
    peers = (
        db.execute(
            select(VideoAnalysis.peak_velocity)
            .join(Video, Video.id == VideoAnalysis.video_id)
            .where(Video.niche_id == video.niche_id)
            .where(VideoAnalysis.peak_velocity.is_not(None))
        )
        .scalars()
        .all()
    )
    if not peers:
        return None
    below = sum(1 for p in peers if p < peak)
    return round(below / len(peers), 4)


def compute_for_seed(view_count: int, hours_since_publish: float | None) -> Velocity:
    """Velocity estimate when only one snapshot exists yet.

    Used as a coarse fallback so freshly-ingested rows have *something*
    to sort by until the bridge has captured a second snapshot.
    """
    if not hours_since_publish or hours_since_publish <= 0:
        return Velocity(None, None, None, None)
    rate = max(0.0, view_count / hours_since_publish)
    return Velocity(
        v24=rate if hours_since_publish <= 24 else None,
        v72=rate if hours_since_publish <= 72 else None,
        peak=rate,
        niche_percentile=None,
    )
