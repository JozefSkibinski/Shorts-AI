"""Celery app + task registry."""

from __future__ import annotations

import logging

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings


log = logging.getLogger(__name__)
settings = get_settings()

celery = Celery(
    "shorts_ai",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery.conf.update(
    task_default_queue="shorts_ai",
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=600,
    task_soft_time_limit=540,
)

# Nightly near-dup clustering. Run with: celery -A workers.celery_app beat
celery.conf.beat_schedule = {
    "cluster-near-dups-nightly": {
        "task": "cluster_unassigned",
        "schedule": crontab(minute="10", hour="3"),
    },
}


@celery.task(name="enrich_video", bind=True, max_retries=3, default_retry_delay=60)
def enrich_video(self, video_pk: int) -> dict:
    """Celery wrapper around app.pipeline.enrich.run_enrichment."""
    from app.pipeline.enrich import run_enrichment

    try:
        return run_enrichment(video_pk)
    except Exception as exc:
        log.exception("enrich_video failed for %s; retrying.", video_pk)
        raise self.retry(exc=exc)


@celery.task(name="cluster_unassigned")
def cluster_unassigned(batch_limit: int = 5000) -> int:
    """Run near-dup clustering for any analyzed video lacking a cluster_id."""
    from app.db.session import session_scope
    from app.ingestor.near_dup import run_unassigned

    with session_scope() as db:
        return run_unassigned(db, limit=batch_limit)
