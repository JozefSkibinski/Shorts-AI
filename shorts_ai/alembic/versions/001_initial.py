"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-04-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector


revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "niches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.Text, unique=True, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
    )

    op.create_table(
        "videos",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("source_video_id", sa.Text, unique=True, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("author_handle", sa.Text),
        sa.Column("author_id", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("hashtags", postgresql.ARRAY(sa.Text)),
        sa.Column("duration_seconds", sa.Float),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "scraped_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.Column("niche_id", sa.Integer, sa.ForeignKey("niches.id")),
        sa.Column("raw_scraper_payload", postgresql.JSONB),
    )
    op.create_index("idx_videos_niche", "videos", ["niche_id"])
    op.create_index(
        "idx_videos_published",
        "videos",
        [sa.text("published_at DESC")],
    )

    op.create_table(
        "video_metrics_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "video_id",
            sa.BigInteger,
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.Column("view_count", sa.BigInteger),
        sa.Column("like_count", sa.BigInteger),
        sa.Column("comment_count", sa.BigInteger),
        sa.Column("hours_since_publish", sa.Float),
    )
    op.create_index(
        "idx_metrics_video",
        "video_metrics_snapshots",
        ["video_id", "captured_at"],
    )

    op.create_table(
        "video_analyses",
        sa.Column(
            "video_id",
            sa.BigInteger,
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("transcript", sa.Text),
        sa.Column("transcript_segments", postgresql.JSONB),
        sa.Column("hook_text", sa.Text),
        sa.Column("hook_type", sa.Text),
        sa.Column("audio_fingerprint", sa.Text),
        sa.Column("audio_is_trending", sa.Boolean),
        sa.Column("audio_track_name", sa.Text),
        sa.Column("visual_caption", sa.Text),
        sa.Column("text_embedding", Vector(384)),
        sa.Column("visual_embedding", Vector(512)),
        sa.Column("velocity_24h", sa.Float),
        sa.Column("velocity_72h", sa.Float),
        sa.Column("peak_velocity", sa.Float),
        sa.Column("niche_percentile", sa.Float),
        sa.Column(
            "analyzed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
    )
    op.execute(
        "CREATE INDEX idx_va_text_emb ON video_analyses "
        "USING hnsw (text_embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX idx_va_visual_emb ON video_analyses "
        "USING hnsw (visual_embedding vector_cosine_ops)"
    )

    op.create_table(
        "user_video_analyses",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.Text),
        sa.Column("source_url", sa.Text),
        sa.Column("analysis", postgresql.JSONB),
        sa.Column("agent_conversation", postgresql.JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("user_video_analyses")
    op.drop_table("video_analyses")
    op.drop_index("idx_metrics_video", table_name="video_metrics_snapshots")
    op.drop_table("video_metrics_snapshots")
    op.drop_index("idx_videos_published", table_name="videos")
    op.drop_index("idx_videos_niche", table_name="videos")
    op.drop_table("videos")
    op.drop_table("niches")
