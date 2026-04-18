"""ACRCloud-backed trending-audio identification.

Degrades gracefully: if ACRCloud creds are missing or the lookup fails,
returns ``{}`` and the rest of the pipeline carries on with audio_track_name
left null.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from app.config import get_settings


log = logging.getLogger(__name__)


def fingerprint(video_path: Path, seconds: int = 15) -> str | None:
    """Return a stable identifier for the audio track.

    For now this is a short SHA-256 over the first ``seconds`` of the
    decoded audio. It's enough to deduplicate "same audio, different
    Short" within the corpus even if ACRCloud isn't configured.
    """
    try:
        import av
        import numpy as np
    except ImportError:
        return None

    try:
        container = av.open(str(video_path))
        try:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                return None
            samples: list[bytes] = []
            collected = 0.0
            for frame in container.decode(stream):
                arr = frame.to_ndarray()
                samples.append(arr.tobytes())
                collected += float(frame.samples) / float(frame.sample_rate or 1)
                if collected >= seconds:
                    break
        finally:
            container.close()
    except Exception as exc:
        log.debug("Audio fingerprint decode failed: %s", exc)
        return None

    if not samples:
        return None
    h = hashlib.sha256()
    for s in samples:
        h.update(s)
    return h.hexdigest()


def lookup(audio_fingerprint: str | None) -> dict:
    """Return {track, artist, is_trending} or {} when no provider is configured."""
    settings = get_settings()
    if not settings.has_acrcloud:
        return {}
    # Real ACRCloud lookup would go here — left as a clean integration point so
    # tests don't pull in network deps. The shape returned is what enrich
    # consumes:
    #   {"track": "...", "artist": "...", "is_trending": bool}
    log.debug(
        "ACRCloud lookup not yet implemented (fingerprint=%s).",
        audio_fingerprint,
    )
    return {}
