"""faster-whisper wrapper. Returns Whisper-style segment dicts."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.config import get_settings


log = logging.getLogger(__name__)


_MODEL: Any = None
_MODEL_NAME: str | None = None


def _get_model():
    global _MODEL, _MODEL_NAME
    settings = get_settings()
    if _MODEL is not None and _MODEL_NAME == settings.whisper_model:
        return _MODEL
    from faster_whisper import WhisperModel

    log.info("Loading Whisper model %s on %s", settings.whisper_model, settings.whisper_device)
    _MODEL = WhisperModel(
        settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )
    _MODEL_NAME = settings.whisper_model
    return _MODEL


def run(video_path: Path) -> list[dict]:
    """Return [{start, end, text}] segments for the audio of ``video_path``."""
    model = _get_model()
    segments, _info = model.transcribe(
        str(video_path),
        beam_size=1,
        vad_filter=True,
        language=None,
    )
    out: list[dict] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        out.append(
            {"start": float(seg.start or 0.0), "end": float(seg.end or 0.0), "text": text}
        )
    return out


def hook_text(segments: list[dict], window_seconds: float = 3.0) -> str:
    parts = [s["text"] for s in segments if s.get("start", 0.0) < window_seconds]
    return " ".join(p.strip() for p in parts).strip()
