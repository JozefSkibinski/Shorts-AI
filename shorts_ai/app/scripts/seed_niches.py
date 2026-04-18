"""Idempotently seed the niches table from NICHE_TAXONOMY."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.models import Niche
from app.db.session import session_scope
from app.pipeline.niche import NICHE_TAXONOMY


log = logging.getLogger(__name__)


def seed() -> int:
    inserted = 0
    with session_scope() as db:
        existing = {
            slug for (slug,) in db.execute(select(Niche.slug)).all()
        }
        for spec in NICHE_TAXONOMY:
            if spec.slug in existing:
                continue
            db.add(
                Niche(
                    slug=spec.slug,
                    display_name=spec.display_name,
                    description=spec.description,
                )
            )
            inserted += 1
    return inserted


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    n = seed()
    log.info("Seeded %d new niches (taxonomy total: %d).", n, len(NICHE_TAXONOMY))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
