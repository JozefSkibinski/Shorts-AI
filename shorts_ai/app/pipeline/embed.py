"""Text and visual embedding helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.config import get_settings


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Text embedding (sentence-transformers / bge-small)
# ---------------------------------------------------------------------------

_TEXT_MODEL: Any = None


def _get_text_model():
    global _TEXT_MODEL
    if _TEXT_MODEL is not None:
        return _TEXT_MODEL
    from sentence_transformers import SentenceTransformer

    name = get_settings().text_embed_model
    log.info("Loading text embedder %s", name)
    _TEXT_MODEL = SentenceTransformer(name)
    return _TEXT_MODEL


def text(content: str) -> list[float]:
    if not content or not content.strip():
        # bge-small dim 384 — return zero vector rather than failing
        return [0.0] * 384
    model = _get_text_model()
    vec = model.encode([content], normalize_embeddings=True)[0]
    return vec.tolist()


# ---------------------------------------------------------------------------
# Visual embedding (open_clip ViT-B/32)
# ---------------------------------------------------------------------------

_CLIP_BUNDLE: tuple[Any, Any, Any] | None = None


def _get_clip():
    global _CLIP_BUNDLE
    if _CLIP_BUNDLE is not None:
        return _CLIP_BUNDLE
    import open_clip
    import torch

    settings = get_settings()
    log.info("Loading CLIP %s / %s", settings.clip_model, settings.clip_pretrained)
    model, _, preprocess = open_clip.create_model_and_transforms(
        settings.clip_model, pretrained=settings.clip_pretrained
    )
    model.eval()
    _CLIP_BUNDLE = (model, preprocess, torch)
    return _CLIP_BUNDLE


def _decode_frame(video_path: Path, t_seconds: float):
    """Return a PIL image at t_seconds from the video. Uses PyAV."""
    import av
    from PIL import Image

    container = av.open(str(video_path))
    try:
        stream = container.streams.video[0]
        # Seek to nearest keyframe
        target = int(t_seconds / stream.time_base)
        container.seek(target, any_frame=False, backward=True, stream=stream)
        for frame in container.decode(stream):
            if float(frame.pts * stream.time_base) >= t_seconds:
                return Image.fromarray(frame.to_ndarray(format="rgb24"))
        # If we ran out, take the last decoded frame
        for frame in container.decode(stream):
            return Image.fromarray(frame.to_ndarray(format="rgb24"))
    finally:
        container.close()
    return None


def visual(video_path: Path, sample_seconds: tuple[float, ...] = (0.5, 3.0, None)) -> list[float]:
    """Average CLIP embedding over a few sampled frames.

    None as a sample point means "midpoint" (computed from container duration).
    Returns a 512-dim vector. On any failure returns zeros so the row still
    gets stored — visual embedding is one signal among many.
    """
    try:
        model, preprocess, torch = _get_clip()
    except Exception as exc:
        log.warning("CLIP not available: %s", exc)
        return [0.0] * 512

    try:
        import av
        container = av.open(str(video_path))
        try:
            duration = float(container.duration or 0) / 1_000_000.0
        finally:
            container.close()

        points: list[float] = []
        for s in sample_seconds:
            if s is None:
                points.append(max(0.5, duration / 2.0))
            elif s < duration or duration <= 0:
                points.append(s)

        frames = []
        for t in points:
            img = _decode_frame(video_path, t)
            if img is not None:
                frames.append(preprocess(img))
        if not frames:
            return [0.0] * 512

        batch = torch.stack(frames)
        with torch.no_grad():
            feats = model.encode_image(batch)
            feats = feats / feats.norm(dim=-1, keepdim=True)
            mean = feats.mean(dim=0)
            mean = mean / mean.norm()
        return mean.tolist()
    except Exception as exc:
        log.warning("Visual embedding failed for %s: %s", video_path, exc)
        return [0.0] * 512
