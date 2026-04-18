"""Download a single Short to a temp dir using yt-dlp."""

from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


log = logging.getLogger(__name__)


@contextmanager
def download(url: str) -> Iterator[Path]:
    """Yield a Path to the downloaded video file. Cleans up on exit."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp is required for enrichment downloads") from exc

    with tempfile.TemporaryDirectory(prefix="shorts_ai_") as tmp:
        tmp_path = Path(tmp)
        opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "bv*+ba/best",
            "outtmpl": str(tmp_path / "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "socket_timeout": 30,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        # Find whatever file landed
        vid = info.get("id") if isinstance(info, dict) else None
        candidate = None
        if vid:
            for f in tmp_path.glob(f"{vid}.*"):
                candidate = f
                break
        if not candidate:
            files = list(tmp_path.iterdir())
            if not files:
                raise RuntimeError(f"yt-dlp produced no file for {url}")
            candidate = files[0]
        yield candidate
