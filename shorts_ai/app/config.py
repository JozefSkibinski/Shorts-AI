"""Centralised settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = ""
    database_url: str = (
        "postgresql+psycopg://shorts:shorts@localhost:5432/shorts_ai"
    )
    redis_url: str = "redis://localhost:6379/0"

    acrcloud_access_key: str = ""
    acrcloud_access_secret: str = ""
    acrcloud_host: str = ""

    whisper_model: str = "small.en"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    text_embed_model: str = "BAAI/bge-small-en-v1.5"
    clip_model: str = "ViT-B-32"
    clip_pretrained: str = "laion2b_s34b_b79k"

    scraper_output_path: Path = Field(default=Path("output/shorts.jsonl"))
    bridge_poll_seconds: int = 30

    niche_model: str = "claude-haiku-4-5-20251001"
    agent_model: str = "claude-opus-4-7"

    @property
    def has_acrcloud(self) -> bool:
        return bool(
            self.acrcloud_access_key
            and self.acrcloud_access_secret
            and self.acrcloud_host
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
