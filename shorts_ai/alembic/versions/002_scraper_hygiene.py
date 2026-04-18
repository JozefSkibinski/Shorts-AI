"""scraper hygiene: is_ad, session tracking, ad verdicts

Revision ID: 002
Revises: 001
Create Date: 2026-04-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # scraper_sessions is referenced by videos.session_id, so create it first.
    op.create_table(
        "scraper_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("user_agent", sa.Text),
        sa.Column("viewport", sa.Text),
        sa.Column("videos_scraped", sa.Integer, server_default="0"),
        sa.Column("ads_filtered", sa.Integer, server_default="0"),
        sa.Column("unique_channels_seen", sa.Integer, server_default="0"),
        sa.Column("channels_distribution", postgresql.JSONB),
        sa.Column("niche_distribution", postgresql.JSONB),
    )
    op.create_index(
        "idx_scraper_sessions_created",
        "scraper_sessions",
        [sa.text("created_at DESC")],
    )

    op.add_column(
        "videos",
        sa.Column("is_ad", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "videos",
        sa.Column(
            "has_paid_promotion_disclosure",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "videos",
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scraper_sessions.id", ondelete="SET NULL"),
        ),
    )
    op.create_index("idx_videos_session", "videos", ["session_id"])
    op.create_index("idx_videos_is_ad", "videos", ["is_ad"])

    op.create_table(
        "video_ad_verdicts",
        sa.Column(
            "video_id",
            sa.BigInteger,
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("is_ad", sa.Boolean, nullable=False),
        sa.Column("reasons", postgresql.ARRAY(sa.Text)),
        sa.Column(
            "classified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("video_ad_verdicts")
    op.drop_index("idx_videos_is_ad", table_name="videos")
    op.drop_index("idx_videos_session", table_name="videos")
    op.drop_column("videos", "session_id")
    op.drop_column("videos", "has_paid_promotion_disclosure")
    op.drop_column("videos", "is_ad")
    op.drop_index("idx_scraper_sessions_created", table_name="scraper_sessions")
    op.drop_table("scraper_sessions")
