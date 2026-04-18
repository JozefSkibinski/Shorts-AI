"""Near-duplicate clustering.

Identical content reposted under different ``source_video_id`` should land
in the same ``content_clusters`` row so the agent doesn't recommend the same
clip 15 different ways.

Signals used (spec 5.1), all cheap once enrichment already ran:

- ``phash_first``, ``phash_mid`` — 64-bit perceptual hashes (Hamming distance)
- ``audio_chromaprint`` — Chromaprint fingerprint (exact-equals bucket)
- CLIP ``visual_embedding`` — cosine distance via pgvector
- sentence-transformer ``text_embedding`` — cosine distance via pgvector

We require **two** strong signals agreeing before clustering, so noisy
short clips (same music, different video) don't collapse by accident.

Designed to run nightly via Celery beat. Inline is safe but wasteful —
it's an N-vs-N job and you're not time-sensitive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.db.models import ContentCluster, Video, VideoAnalysis


log = logging.getLogger(__name__)


# Thresholds match the spec's confirm rules. Tune from real data, not vibes.
PHASH_HAMMING_MAX = 5
VISUAL_COS_MAX = 0.08
TEXT_COS_MAX = 0.10
CANDIDATE_LIMIT = 50
MIN_CONFIRMING_SIGNALS = 2


def _hamming64(a: int | None, b: int | None) -> int | None:
    if a is None or b is None:
        return None
    return bin((int(a) ^ int(b)) & ((1 << 64) - 1)).count("1")


def _cosine_unit(a: list[float] | None, b: list[float] | None) -> float | None:
    """Cosine distance (1 - cos similarity) of unit vectors. ``None`` if either is zero."""
    if not a or not b:
        return None
    # vectors from pgvector come back as list[float]
    num = sum(x * y for x, y in zip(a, b))
    # both are normalized elsewhere (sentence-transformers normalize_embeddings=True,
    # CLIP we normalize in embed.visual). Be lenient about inputs that aren't.
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return None
    return max(0.0, 1.0 - (num / (mag_a * mag_b)))


@dataclass
class Signals:
    phash_first_hamming: int | None
    phash_mid_hamming: int | None
    audio_match: bool
    visual_cos: float | None
    text_cos: float | None

    def confirming_count(self) -> int:
        """Count strong-agreement signals between a pair (spec 5.3)."""
        n = 0
        if (
            self.phash_first_hamming is not None
            and self.phash_first_hamming < PHASH_HAMMING_MAX
        ):
            n += 1
        if self.audio_match:
            n += 1
        if self.visual_cos is not None and self.visual_cos < VISUAL_COS_MAX:
            n += 1
        if self.text_cos is not None and self.text_cos < TEXT_COS_MAX:
            n += 1
        return n


def compare(a: VideoAnalysis, b: VideoAnalysis) -> Signals:
    return Signals(
        phash_first_hamming=_hamming64(a.phash_first, b.phash_first),
        phash_mid_hamming=_hamming64(a.phash_mid, b.phash_mid),
        audio_match=bool(
            a.audio_chromaprint
            and b.audio_chromaprint
            and a.audio_chromaprint == b.audio_chromaprint
        ),
        visual_cos=_cosine_unit(a.visual_embedding, b.visual_embedding),
        text_cos=_cosine_unit(a.text_embedding, b.text_embedding),
    )


def confirm_match(a: VideoAnalysis, b: VideoAnalysis) -> bool:
    return compare(a, b).confirming_count() >= MIN_CONFIRMING_SIGNALS


# ---------------------------------------------------------------------------
# Candidate search — narrows N-vs-N to a tractable set per probe
# ---------------------------------------------------------------------------


def find_cluster_candidates(db: Session, va: VideoAnalysis) -> set[int]:
    """Return video_ids with any one cheap signal in range. Confirm later."""
    candidates: set[int] = set()

    if va.phash_first is not None:
        rows = db.execute(
            text(
                "SELECT video_id FROM video_analyses "
                "WHERE phash_first IS NOT NULL "
                "  AND abs(phash_first - :p) < :threshold"
            ),
            {"p": int(va.phash_first), "threshold": 1 << 20},
        ).scalars().all()
        candidates.update(int(r) for r in rows)

    if va.audio_chromaprint:
        rows = db.execute(
            text(
                "SELECT video_id FROM video_analyses "
                "WHERE audio_chromaprint = :fp"
            ),
            {"fp": va.audio_chromaprint},
        ).scalars().all()
        candidates.update(int(r) for r in rows)

    if va.visual_embedding is not None:
        rows = db.execute(
            text(
                "SELECT video_id FROM video_analyses "
                "WHERE visual_embedding IS NOT NULL "
                "ORDER BY visual_embedding <=> :emb "
                "LIMIT :lim"
            ),
            {"emb": list(va.visual_embedding), "lim": CANDIDATE_LIMIT},
        ).scalars().all()
        candidates.update(int(r) for r in rows)

    candidates.discard(int(va.video_id))
    return candidates


# ---------------------------------------------------------------------------
# Cluster assignment
# ---------------------------------------------------------------------------


def _cluster_of(db: Session, video_id: int) -> int | None:
    return db.execute(
        select(Video.content_cluster_id).where(Video.id == video_id)
    ).scalar_one_or_none()


def _merge_clusters(db: Session, keep: int, drop: int) -> None:
    """Move every video in cluster ``drop`` into cluster ``keep``."""
    if keep == drop:
        return
    db.execute(
        update(Video)
        .where(Video.content_cluster_id == drop)
        .values(content_cluster_id=keep)
    )
    # Size + drop the emptied cluster.
    db.execute(
        update(ContentCluster)
        .where(ContentCluster.id == keep)
        .values(size=ContentCluster.size + _cluster_size(db, drop))
    )
    db.execute(
        ContentCluster.__table__.delete().where(ContentCluster.id == drop)
    )


def _cluster_size(db: Session, cluster_id: int) -> int:
    return db.execute(
        select(ContentCluster.size).where(ContentCluster.id == cluster_id)
    ).scalar_one_or_none() or 0


def assign_cluster(db: Session, va: VideoAnalysis) -> int:
    """Place ``va``'s video into an existing cluster or make a new singleton.

    Returns the cluster id the video ended up in.
    """
    video_id = int(va.video_id)
    existing = _cluster_of(db, video_id)
    if existing is not None:
        return existing

    matched_clusters: set[int] = set()
    for cand_id in find_cluster_candidates(db, va):
        cand = db.get(VideoAnalysis, cand_id)
        if cand is None:
            continue
        if not confirm_match(va, cand):
            continue
        c_id = _cluster_of(db, cand_id)
        if c_id is not None:
            matched_clusters.add(c_id)

    if not matched_clusters:
        cluster = ContentCluster(representative_video_id=video_id, size=1)
        db.add(cluster)
        db.flush()
        db.execute(
            update(Video).where(Video.id == video_id).values(content_cluster_id=cluster.id)
        )
        return cluster.id

    # Keep the oldest cluster (smallest id), merge the rest into it.
    keep = min(matched_clusters)
    for drop in matched_clusters - {keep}:
        _merge_clusters(db, keep, drop)

    db.execute(
        update(Video).where(Video.id == video_id).values(content_cluster_id=keep)
    )
    db.execute(
        update(ContentCluster)
        .where(ContentCluster.id == keep)
        .values(size=ContentCluster.size + 1)
    )
    _refresh_representative(db, keep)
    return keep


def _refresh_representative(db: Session, cluster_id: int) -> None:
    """The representative video is the one with the highest peak_velocity."""
    best = db.execute(
        text(
            "SELECT v.id FROM videos v "
            "JOIN video_analyses va ON va.video_id = v.id "
            "WHERE v.content_cluster_id = :cid "
            "ORDER BY va.peak_velocity DESC NULLS LAST "
            "LIMIT 1"
        ),
        {"cid": cluster_id},
    ).scalar_one_or_none()
    if best:
        db.execute(
            update(ContentCluster)
            .where(ContentCluster.id == cluster_id)
            .values(representative_video_id=int(best))
        )


# ---------------------------------------------------------------------------
# Nightly batch runner
# ---------------------------------------------------------------------------


def run_unassigned(db: Session, *, limit: int = 5000) -> int:
    """Assign clusters to every VideoAnalysis whose video lacks a content_cluster_id.

    Returns the number of videos processed. Suitable for a Celery beat task
    scheduled once a day — call in a loop until it returns 0 if the backlog
    is large.
    """
    pending = db.execute(
        text(
            "SELECT va.video_id FROM video_analyses va "
            "JOIN videos v ON v.id = va.video_id "
            "WHERE v.content_cluster_id IS NULL "
            "LIMIT :lim"
        ),
        {"lim": limit},
    ).scalars().all()
    n = 0
    for vid in pending:
        va = db.get(VideoAnalysis, int(vid))
        if va is None:
            continue
        try:
            assign_cluster(db, va)
            n += 1
        except Exception as exc:
            log.exception("cluster assignment failed for %s: %s", vid, exc)
    return n
