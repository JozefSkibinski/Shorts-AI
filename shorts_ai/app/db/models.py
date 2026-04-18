"""SQLAlchemy models matching alembic/versions/001_initial.sql."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


TEXT_EMBED_DIM = 384   # bge-small-en-v1.5
VISUAL_EMBED_DIM = 512  # CLIP ViT-B/32


class Base(DeclarativeBase):
    pass


class Niche(Base):
    __tablename__ = "niches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_video_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    author_handle: Mapped[str | None] = mapped_column(String)
    author_id: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    niche_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id"))
    raw_scraper_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    snapshots: Mapped[list["VideoMetricsSnapshot"]] = relationship(
        back_populates="video",
        cascade="all, delete-orphan",
        order_by="VideoMetricsSnapshot.captured_at",
    )
    analysis: Mapped["VideoAnalysis | None"] = relationship(
        back_populates="video", cascade="all, delete-orphan", uselist=False
    )


class VideoMetricsSnapshot(Base):
    __tablename__ = "video_metrics_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    view_count: Mapped[int | None] = mapped_column(BigInteger)
    like_count: Mapped[int | None] = mapped_column(BigInteger)
    comment_count: Mapped[int | None] = mapped_column(BigInteger)
    hours_since_publish: Mapped[float | None] = mapped_column(Float)

    video: Mapped[Video] = relationship(back_populates="snapshots")


class VideoAnalysis(Base):
    __tablename__ = "video_analyses"

    video_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("videos.id", ondelete="CASCADE"),
        primary_key=True,
    )
    transcript: Mapped[str | None] = mapped_column(Text)
    transcript_segments: Mapped[list[dict] | None] = mapped_column(JSONB)
    hook_text: Mapped[str | None] = mapped_column(Text)
    hook_type: Mapped[str | None] = mapped_column(String)
    audio_fingerprint: Mapped[str | None] = mapped_column(Text)
    audio_is_trending: Mapped[bool | None] = mapped_column(Boolean)
    audio_track_name: Mapped[str | None] = mapped_column(Text)
    visual_caption: Mapped[str | None] = mapped_column(Text)
    text_embedding: Mapped[list[float] | None] = mapped_column(Vector(TEXT_EMBED_DIM))
    visual_embedding: Mapped[list[float] | None] = mapped_column(
        Vector(VISUAL_EMBED_DIM)
    )
    velocity_24h: Mapped[float | None] = mapped_column(Float)
    velocity_72h: Mapped[float | None] = mapped_column(Float)
    peak_velocity: Mapped[float | None] = mapped_column(Float)
    niche_percentile: Mapped[float | None] = mapped_column(Float)
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    video: Mapped[Video] = relationship(back_populates="analysis")


class UserVideoAnalysis(Base):
    __tablename__ = "user_video_analyses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String)
    source_url: Mapped[str | None] = mapped_column(Text)
    analysis: Mapped[dict | None] = mapped_column(JSONB)
    agent_conversation: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
