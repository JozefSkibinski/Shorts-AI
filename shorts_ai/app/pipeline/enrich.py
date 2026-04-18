"""Enrichment orchestrator: download -> transcribe -> embed -> persist."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Niche, Video, VideoAnalysis
from app.db.session import session_scope
from app.pipeline import audio_id, embed, hook, metrics, niche, transcribe
from app.pipeline.download import download


log = logging.getLogger(__name__)


def _get_niche_id(db: Session, slug: str) -> int | None:
    if not slug:
        return None
    row = db.execute(select(Niche.id).where(Niche.slug == slug)).first()
    return row[0] if row else None


def _existing_analysis(db: Session, video_pk: int) -> VideoAnalysis | None:
    return db.get(VideoAnalysis, video_pk)


def run_enrichment(video_pk: int, *, force: bool = False) -> dict:
    """Run the full enrichment pipeline for one video. Returns a status dict."""
    with session_scope() as db:
        video = db.get(Video, video_pk)
        if video is None:
            return {"video_id": video_pk, "status": "missing"}

        if not force and _existing_analysis(db, video_pk) is not None:
            return {"video_id": video_pk, "status": "already_analyzed"}

        url = video.url
        title = video.title or ""
        hashtags = list(video.hashtags or [])

    # Heavy work outside the session; reopen on commit
    transcript_text = ""
    segments: list[dict] = []
    hook_text = ""
    audio_fp: str | None = None
    audio_meta: dict = {}
    visual_emb: list[float] = []

    try:
        with download(url) as video_path:
            try:
                segments = transcribe.run(video_path)
                transcript_text = " ".join(s["text"] for s in segments).strip()
                hook_text = transcribe.hook_text(segments, window_seconds=3.0)
            except Exception as exc:
                log.warning("Transcription failed for video %s: %s", video_pk, exc)

            try:
                audio_fp = audio_id.fingerprint(video_path)
                if audio_fp:
                    audio_meta = audio_id.lookup(audio_fp)
            except Exception as exc:
                log.warning("Audio identification failed for %s: %s", video_pk, exc)

            try:
                visual_emb = embed.visual(video_path)
            except Exception as exc:
                log.warning("Visual embedding failed for %s: %s", video_pk, exc)
    except Exception as exc:
        log.exception("Download failed for video %s: %s", video_pk, exc)
        return {"video_id": video_pk, "status": "download_failed", "error": str(exc)}

    text_blob = "\n".join(
        x for x in (title, hook_text, transcript_text, " ".join(hashtags)) if x
    )
    text_emb = embed.text(text_blob) if text_blob else [0.0] * 384

    hook_label = hook.classify(hook_text) if hook_text else "visual"
    niche_slug = niche.classify(title, transcript_text, hashtags)

    with session_scope() as db:
        video = db.get(Video, video_pk)
        if video is None:
            return {"video_id": video_pk, "status": "missing_after_download"}

        if video.niche_id is None:
            video.niche_id = _get_niche_id(db, niche_slug)

        vel = metrics.compute(db, video_pk)

        analysis = _existing_analysis(db, video_pk) or VideoAnalysis(video_id=video_pk)
        analysis.transcript = transcript_text or None
        analysis.transcript_segments = segments or None
        analysis.hook_text = hook_text or None
        analysis.hook_type = hook_label
        analysis.audio_fingerprint = audio_fp
        analysis.audio_track_name = audio_meta.get("track")
        analysis.audio_is_trending = bool(audio_meta.get("is_trending", False))
        analysis.text_embedding = text_emb
        analysis.visual_embedding = visual_emb or None
        analysis.velocity_24h = vel.v24
        analysis.velocity_72h = vel.v72
        analysis.peak_velocity = vel.peak
        analysis.niche_percentile = vel.niche_percentile
        db.merge(analysis)

    return {
        "video_id": video_pk,
        "status": "ok",
        "niche": niche_slug,
        "hook_type": hook_label,
        "transcript_chars": len(transcript_text),
        "has_visual_emb": bool(visual_emb),
        "has_text_emb": True,
        "audio_track": audio_meta.get("track"),
    }
