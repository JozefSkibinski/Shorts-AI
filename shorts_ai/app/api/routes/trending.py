"""GET /trending/{niche} — top recent Shorts in a niche by peak velocity.

Phase 1: read-only over what the bridge + enrichment have populated.
Phase 2 will add /chat and /analyze.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.db.models import Niche, Video, VideoAnalysis
from app.db.session import session_scope


router = APIRouter()


@router.get("/trending/{niche_slug}")
def trending(
    niche_slug: str,
    timeframe_hours: int = Query(72, ge=1, le=24 * 30),
    limit: int = Query(10, ge=1, le=100),
) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=timeframe_hours)
    with session_scope() as db:
        niche_id = db.execute(
            select(Niche.id).where(Niche.slug == niche_slug)
        ).scalar_one_or_none()
        if niche_id is None:
            raise HTTPException(404, f"unknown niche: {niche_slug}")

        rows = db.execute(
            select(
                Video.source_video_id,
                Video.url,
                Video.title,
                Video.author_handle,
                VideoAnalysis.peak_velocity,
                VideoAnalysis.velocity_24h,
                VideoAnalysis.niche_percentile,
            )
            .join(VideoAnalysis, VideoAnalysis.video_id == Video.id)
            .where(Video.niche_id == niche_id)
            .where(Video.scraped_at >= cutoff)
            .order_by(VideoAnalysis.peak_velocity.desc().nullslast())
            .limit(limit)
        ).all()

    return {
        "niche": niche_slug,
        "timeframe_hours": timeframe_hours,
        "count": len(rows),
        "videos": [
            {
                "id": r.source_video_id,
                "url": r.url,
                "title": r.title,
                "channel": r.author_handle,
                "peak_velocity": r.peak_velocity,
                "velocity_24h": r.velocity_24h,
                "niche_percentile": r.niche_percentile,
            }
            for r in rows
        ],
    }
