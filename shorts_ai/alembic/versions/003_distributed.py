"""distributed scraping + near-dup clustering

Revision ID: 003
Revises: 002
Create Date: 2026-04-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- scraper_machines -------------------------------------------------
    op.create_table(
        "scraper_machines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hostname", sa.Text),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("total_batches", sa.Integer, server_default="0"),
        sa.Column("scraper_version", sa.Text),
    )

    # ---- scraper_sessions.machine_id -------------------------------------
    op.add_column(
        "scraper_sessions",
        sa.Column(
            "machine_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scraper_machines.id", ondelete="SET NULL"),
        ),
    )
    op.create_index(
        "idx_scraper_sessions_machine", "scraper_sessions", ["machine_id"]
    )

    # ---- videos: first/last seen + cluster -------------------------------
    op.add_column("videos", sa.Column("first_seen_at", sa.DateTime(timezone=True)))
    op.add_column("videos", sa.Column("last_seen_at", sa.DateTime(timezone=True)))
    op.add_column(
        "videos",
        sa.Column(
            "first_seen_by_session",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scraper_sessions.id", ondelete="SET NULL"),
        ),
    )
    op.create_index("idx_videos_first_seen", "videos", ["first_seen_at"])

    # ---- content_clusters -------------------------------------------------
    op.create_table(
        "content_clusters",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("representative_video_id", sa.BigInteger),
        sa.Column("size", sa.Integer, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
    )
    op.add_column(
        "videos",
        sa.Column(
            "content_cluster_id",
            sa.BigInteger,
            sa.ForeignKey("content_clusters.id", ondelete="SET NULL"),
        ),
    )
    op.create_index("idx_videos_cluster", "videos", ["content_cluster_id"])

    # FK from content_clusters.representative_video_id back to videos.id; we
    # create it after both tables exist so the dependency resolves cleanly.
    op.create_foreign_key(
        "fk_clusters_representative_video",
        source_table="content_clusters",
        referent_table="videos",
        local_cols=["representative_video_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )

    # ---- video_metrics_snapshots: machine + session tags ------------------
    op.add_column(
        "video_metrics_snapshots",
        sa.Column("source_machine_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "video_metrics_snapshots",
        sa.Column("source_session_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_index(
        "idx_metrics_machine",
        "video_metrics_snapshots",
        ["source_machine_id", "captured_at"],
    )

    # ---- video_analyses: near-dup fingerprints ---------------------------
    op.add_column("video_analyses", sa.Column("phash_first", sa.BigInteger))
    op.add_column("video_analyses", sa.Column("phash_mid", sa.BigInteger))
    op.add_column("video_analyses", sa.Column("audio_chromaprint", sa.Text))
    op.create_index("idx_va_phash_first", "video_analyses", ["phash_first"])
    op.create_index("idx_va_phash_mid", "video_analyses", ["phash_mid"])
    op.create_index("idx_va_chromaprint", "video_analyses", ["audio_chromaprint"])

    # ---- raw_batches (staging audit table) -------------------------------
    op.create_table(
        "raw_batches",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("s3_key", sa.Text, unique=True, nullable=False),
        sa.Column("machine_id", postgresql.UUID(as_uuid=True)),
        sa.Column("session_id", postgresql.UUID(as_uuid=True)),
        sa.Column("uploaded_at", sa.DateTime(timezone=True)),
        sa.Column("ingested_at", sa.DateTime(timezone=True)),
        sa.Column("records", sa.Integer),
        sa.Column(
            "status",
            sa.Text,
            sa.CheckConstraint(
                "status IN ('pending','ingested','failed','quarantined')",
                name="raw_batches_status_check",
            ),
        ),
        sa.Column("error", sa.Text),
    )
    op.create_index("idx_raw_batches_status", "raw_batches", ["status"])
    op.create_index(
        "idx_raw_batches_uploaded", "raw_batches", [sa.text("uploaded_at DESC")]
    )


def downgrade() -> None:
    op.drop_index("idx_raw_batches_uploaded", table_name="raw_batches")
    op.drop_index("idx_raw_batches_status", table_name="raw_batches")
    op.drop_table("raw_batches")

    op.drop_index("idx_va_chromaprint", table_name="video_analyses")
    op.drop_index("idx_va_phash_mid", table_name="video_analyses")
    op.drop_index("idx_va_phash_first", table_name="video_analyses")
    op.drop_column("video_analyses", "audio_chromaprint")
    op.drop_column("video_analyses", "phash_mid")
    op.drop_column("video_analyses", "phash_first")

    op.drop_index("idx_metrics_machine", table_name="video_metrics_snapshots")
    op.drop_column("video_metrics_snapshots", "source_session_id")
    op.drop_column("video_metrics_snapshots", "source_machine_id")

    op.drop_constraint(
        "fk_clusters_representative_video",
        "content_clusters",
        type_="foreignkey",
    )
    op.drop_index("idx_videos_cluster", table_name="videos")
    op.drop_column("videos", "content_cluster_id")
    op.drop_table("content_clusters")

    op.drop_index("idx_videos_first_seen", table_name="videos")
    op.drop_column("videos", "first_seen_by_session")
    op.drop_column("videos", "last_seen_at")
    op.drop_column("videos", "first_seen_at")

    op.drop_index("idx_scraper_sessions_machine", table_name="scraper_sessions")
    op.drop_column("scraper_sessions", "machine_id")

    op.drop_table("scraper_machines")
