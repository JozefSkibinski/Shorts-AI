"""YouTube Shorts scraper.

Opens YouTube Shorts in an incognito Playwright context so browsing does not
influence any logged-in algorithm, auto-scrolls the feed to collect Short
video IDs, and for every Short with at least ``--min-views`` views writes a
human-readable record to ``data.txt``.

Metadata resolution uses three sources in order, so at least one should
always produce an exact ``view_count``:

    1. ``yt-dlp`` extract_info for the video URL (most reliable)
    2. the ``ytInitialPlayerResponse`` embedded in the Short's HTML page
       (fetched via the same incognito context)
    3. ``/youtubei/v1/player`` responses captured by a Playwright response
       listener while scrolling

If all three disagree, yt-dlp wins. If all three return 0, the Short is
logged anyway with ``view_count: 0`` and a clear note so the problem is
visible rather than silent.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Iterable

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

try:
    import yt_dlp
except ImportError:  # runtime check with a clear message
    yt_dlp = None  # type: ignore[assignment]

import ad_filter
import machine_identity
import stats as stats_module
from batch_writer import BatchEnvelope, BatchWriter
from session_manager import SessionManager


SHORTS_URL = "https://www.youtube.com/shorts"
MIN_VIEWS = 1_000_000
HOOK_SECONDS = 7.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

log = logging.getLogger("shorts_scraper")


@dataclass
class ShortRecord:
    video_id: str
    url: str
    channel: str
    title: str
    description: str
    hashtags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    view_count: int = 0
    duration_seconds: float | None = None
    transcript: str | None = None
    hook: str | None = None
    metadata_source: str = ""
    transcript_source: str = "none"
    has_paid_promotion_disclosure: bool = False
    session_id: str = ""
    user_agent: str = ""
    viewport: str = ""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def extract_hashtags(*texts: str | None) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for raw in re.findall(r"#\w+", text):
            tag = raw.lower()
            if tag not in seen:
                seen.add(tag)
                tags.append(raw)
    return tags


def extract_video_id(url: str) -> str | None:
    match = re.search(r"/shorts/([A-Za-z0-9_-]{6,})", url)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Transcript + hook
# ---------------------------------------------------------------------------

# Cached Whisper model (created once per run, lazily)
_WHISPER_MODEL = None
_WHISPER_MODEL_NAME: str | None = None


def _get_whisper_model(name: str):
    """Load a faster-whisper model on first use and cache it."""
    global _WHISPER_MODEL, _WHISPER_MODEL_NAME
    if _WHISPER_MODEL is not None and _WHISPER_MODEL_NAME == name:
        return _WHISPER_MODEL
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    try:
        log.info("Loading Whisper model '%s' (first use downloads weights)...", name)
        _WHISPER_MODEL = WhisperModel(name, device="cpu", compute_type="int8")
        _WHISPER_MODEL_NAME = name
    except Exception as exc:
        log.warning("Could not load Whisper model '%s': %s", name, exc)
        _WHISPER_MODEL = None
    return _WHISPER_MODEL


def transcribe_with_whisper(video_id: str, model_name: str) -> list[dict] | None:
    """Download audio with yt-dlp, transcribe with faster-whisper.

    Returns a list of {text, start, duration} segments compatible with the
    captions path, or None if anything fails.
    """
    if yt_dlp is None:
        return None

    model = _get_whisper_model(model_name)
    if model is None:
        return None

    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opts = {
                "quiet": True,
                "no_warnings": True,
                "format": "bestaudio/best",
                "outtmpl": str(tmp_path / "%(id)s.%(ext)s"),
                "socket_timeout": 30,
            }
            url = f"https://www.youtube.com/shorts/{video_id}"
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)

            audio = next(tmp_path.glob(f"{video_id}.*"), None)
            if audio is None:
                log.debug("no audio file for %s", video_id)
                return None

            segments, _info = model.transcribe(
                str(audio),
                beam_size=1,
                vad_filter=True,
                language=None,
            )
            return [
                {
                    "text": seg.text.strip(),
                    "start": float(seg.start or 0.0),
                    "duration": float((seg.end or 0.0) - (seg.start or 0.0)),
                }
                for seg in segments
                if seg.text and seg.text.strip()
            ]
    except Exception as exc:
        log.debug("whisper transcription failed for %s: %s", video_id, exc)
        return None


def _snippets_to_transcript_and_hook(
    snippets: list[dict],
) -> tuple[str | None, str | None]:
    full = " ".join(s["text"].strip() for s in snippets if s.get("text")).strip()
    hook_parts = [
        s["text"].strip()
        for s in snippets
        if s.get("text") and s.get("start", 0) < HOOK_SECONDS
    ]
    hook = " ".join(hook_parts).strip() or None
    return full or None, hook


def _fetch_captions(video_id: str) -> list[dict] | None:
    """Call youtube-transcript-api on either 0.6 or 1.x."""
    languages = ["en", "en-US", "en-GB"]
    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            return YouTubeTranscriptApi.get_transcript(
                video_id, languages=languages
            )
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=languages)
        if hasattr(fetched, "to_raw_data"):
            return fetched.to_raw_data()
        return [
            {"text": s.text, "start": s.start, "duration": s.duration}
            for s in fetched
        ]
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable):
        return None
    except Exception as exc:
        log.debug("Captions fetch failed for %s: %s", video_id, exc)
        return None


def fetch_transcript(
    video_id: str, whisper_model: str | None
) -> tuple[str | None, str | None, str]:
    """Return (full_transcript, hook, source).

    ``source`` is one of ``captions``, ``whisper``, or ``none``.
    """
    snippets = _fetch_captions(video_id)

    if snippets:
        full, hook = _snippets_to_transcript_and_hook(snippets)
        if full or hook:
            return full, hook, "captions"

    if whisper_model:
        whispered = transcribe_with_whisper(video_id, whisper_model)
        if whispered:
            full, hook = _snippets_to_transcript_and_hook(whispered)
            if full or hook:
                return full, hook, "whisper"

    return None, None, "none"


# ---------------------------------------------------------------------------
# Source 1: yt-dlp (primary)
# ---------------------------------------------------------------------------


def fetch_metadata_ytdlp(video_id: str) -> dict | None:
    if yt_dlp is None:
        return None

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "socket_timeout": 20,
    }
    url = f"https://www.youtube.com/shorts/{video_id}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        log.debug("yt-dlp failed for %s: %s", video_id, exc)
        return None

    if not info:
        return None

    views = info.get("view_count")
    try:
        views_int = int(views) if views is not None else 0
    except (TypeError, ValueError):
        views_int = 0

    duration = info.get("duration")
    try:
        duration_sec = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_sec = None

    tags = info.get("tags") or []
    return {
        "title": (info.get("title") or "").strip(),
        "channel": (info.get("channel") or info.get("uploader") or "").strip(),
        "description": (info.get("description") or "").strip(),
        "view_count": views_int,
        "duration_seconds": duration_sec,
        "keywords": list(tags),
        "source": "yt-dlp",
    }


# ---------------------------------------------------------------------------
# Source 2: HTML page ytInitialPlayerResponse (fallback)
# ---------------------------------------------------------------------------


def _balanced_json(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_player_payload(payload: dict) -> dict:
    details = payload.get("videoDetails") or {}
    try:
        views = int(details.get("viewCount") or 0)
    except (TypeError, ValueError):
        views = 0
    try:
        duration = float(details.get("lengthSeconds") or 0) or None
    except (TypeError, ValueError):
        duration = None

    ad_placements = payload.get("adPlacements") or []
    playability = (payload.get("playabilityStatus") or {}).get("status")

    return {
        "title": (details.get("title") or "").strip(),
        "channel": (details.get("author") or "").strip(),
        "description": (details.get("shortDescription") or "").strip(),
        "view_count": views,
        "duration_seconds": duration,
        "keywords": list(details.get("keywords") or []),
        "ad_placements": bool(ad_placements),
        "is_ad_flag": bool(details.get("isAd")),
        "playability_status": playability,
    }


def parse_initial_player_response(html: str) -> dict | None:
    idx = html.find("ytInitialPlayerResponse")
    if idx == -1:
        return None
    brace = html.find("{", idx)
    if brace == -1:
        return None
    blob = _balanced_json(html, brace)
    if not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def fetch_metadata_html(context: BrowserContext, video_id: str) -> dict | None:
    for path in (f"/shorts/{video_id}", f"/watch?v={video_id}"):
        url = f"https://www.youtube.com{path}"
        try:
            resp = context.request.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=15_000,
            )
            if not resp.ok:
                continue
            html = resp.text()
        except Exception as exc:
            log.debug("HTML fetch failed for %s: %s", url, exc)
            continue
        payload = parse_initial_player_response(html)
        if not payload:
            continue
        fields = _parse_player_payload(payload)
        if fields.get("view_count"):
            fields["source"] = f"html:{path}"
            return fields
    return None


# ---------------------------------------------------------------------------
# Source 3: /youtubei/v1/player response interceptor
# ---------------------------------------------------------------------------


class MetadataStore:
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self._lock = Lock()

    def update(self, video_id: str, fields: dict) -> None:
        with self._lock:
            existing = self._data.setdefault(video_id, {})
            for k, v in fields.items():
                if v not in (None, "", 0) or k not in existing:
                    existing[k] = v

    def get(self, video_id: str) -> dict | None:
        with self._lock:
            return dict(self._data.get(video_id) or {}) or None


def install_player_interceptor(context: BrowserContext, store: MetadataStore) -> None:
    def handle(response: Response) -> None:
        if "/youtubei/v1/player" not in response.url:
            return
        try:
            data = response.json()
        except Exception:
            return
        details = (data or {}).get("videoDetails") or {}
        vid = details.get("videoId")
        if not vid:
            return
        fields = _parse_player_payload(data)
        fields["source"] = "interceptor"
        store.update(vid, fields)
        log.debug("intercepted %s views=%s", vid, fields.get("view_count"))

    context.on("response", handle)


# ---------------------------------------------------------------------------
# Metadata resolver
# ---------------------------------------------------------------------------


def resolve_metadata(
    context: BrowserContext, store: MetadataStore, video_id: str
) -> dict:
    """Try every source; return the best result we can get."""
    # 1. yt-dlp (most reliable, exact integer view_count)
    meta = fetch_metadata_ytdlp(video_id)
    if meta and meta.get("view_count"):
        return meta
    best = meta or {}

    # 2. HTML page parse
    html_meta = fetch_metadata_html(context, video_id)
    if html_meta and html_meta.get("view_count"):
        return html_meta
    if html_meta and not best:
        best = html_meta

    # 3. Interceptor store
    intercepted = store.get(video_id)
    if intercepted and intercepted.get("view_count"):
        intercepted["source"] = intercepted.get("source", "interceptor")
        return intercepted
    if intercepted and not best:
        intercepted["source"] = intercepted.get("source", "interceptor")
        best = intercepted

    if not best:
        best = {"source": "none", "view_count": 0}
    return best


# ---------------------------------------------------------------------------
# Page interaction
# ---------------------------------------------------------------------------


def dismiss_consent(page: Page) -> None:
    selectors = [
        'button[aria-label*="Accept all"]',
        'button[aria-label*="Reject all"]',
        'button:has-text("Accept all")',
        'button:has-text("Reject all")',
        'button:has-text("I agree")',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000):
                btn.click(timeout=2000)
                page.wait_for_timeout(1000)
                return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue


def current_video_id(page: Page) -> str | None:
    try:
        href = page.evaluate("() => location.href")
    except Exception:
        href = ""
    vid = extract_video_id(href or "")
    if vid:
        return vid
    try:
        vid = page.evaluate(
            """() => {
                const a = document.querySelector('ytd-reel-video-renderer[is-active]');
                if (!a) return null;
                return a.getAttribute('video-id') || a.id || null;
            }"""
        )
    except Exception:
        vid = None
    return vid


def context_for_session(sessions: SessionManager) -> BrowserContext:
    """Return the BrowserContext of the currently active session.

    ``resolve_metadata`` needs a context for its HTTP fallback path; this
    lets it share the session's cookies + headers so the request matches
    what the page itself would send.
    """
    current = sessions.current
    if current is None:
        raise RuntimeError("context_for_session called before any session exists")
    return current.context


def _log_ad_drop(
    ads_path: Path,
    video_id: str,
    session_id: str | None,
    verdict: "ad_filter.AdVerdict",
) -> None:
    """Append one line to ads_filtered.jsonl for audit spot-checks."""
    try:
        payload = {
            "video_id": video_id,
            "session_id": session_id or "",
            "reasons": verdict.reasons,
            "url": f"https://www.youtube.com/shorts/{video_id}",
            "ts": time.time(),
        }
        with ads_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.debug("Could not write ads_filtered line: %s", exc)
    log.info(
        "dropped ad %s (reasons=%s)",
        video_id,
        ",".join(verdict.reasons[:3]),
    )


def advance(page: Page, previous_id: str | None) -> str | None:
    """Scroll to the next Short. Retries if the page doesn't move."""
    for attempt in range(4):
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(random.randint(1500, 2800))
        new_id = current_video_id(page)
        if new_id and new_id != previous_id:
            return new_id
        # stronger nudge: wheel the viewport
        try:
            page.mouse.wheel(0, 900)
        except Exception:
            pass
        page.wait_for_timeout(700)
    return current_video_id(page)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "unknown"
    s = int(round(seconds))
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"


def _compact(text: str) -> str:
    """Collapse whitespace so transcripts fit on a single line."""
    return re.sub(r"\s+", " ", text).strip()


def format_record(record: ShortRecord) -> str:
    """Compact one-block-per-Short format. Empty fields are omitted."""
    parts: list[str] = []
    parts.append(
        f"[{record.view_count:,}v {format_duration(record.duration_seconds)}] "
        f"{record.channel or '?'} | {record.title or '?'}"
    )
    parts.append(f"  url:  {record.url}")
    if record.hashtags:
        parts.append(f"  tags: {' '.join(record.hashtags)}")
    src = f"{record.metadata_source or '?'} / {record.transcript_source}"
    parts.append(f"  src:  {src}")
    desc = _compact(record.description)
    if desc:
        parts.append(f"  desc: {desc}")
    if record.hook:
        parts.append(f"  hook: {_compact(record.hook)}")
    if record.transcript and record.transcript != record.hook:
        parts.append(f"  text: {_compact(record.transcript)}")
    parts.append("--")
    return "\n".join(parts) + "\n"


def append_record(data_path: Path, jsonl_path: Path, record: ShortRecord) -> None:
    with data_path.open("a", encoding="utf-8") as f:
        f.write(format_record(record))
        f.write("\n")
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def load_previous_ids(jsonl_path: Path) -> set[str]:
    """Read any existing jsonl file so previously-saved Shorts are skipped."""
    if not jsonl_path.exists():
        return set()
    ids: set[str] = set()
    with jsonl_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            vid = row.get("video_id")
            if vid:
                ids.add(vid)
    return ids


def append_summary(
    data_path: Path,
    scanned: int,
    kept: int,
    skipped_ads: int,
    channel_counts: dict[str, int],
    transcript_counts: dict[str, int],
) -> None:
    top_channels = sorted(channel_counts.items(), key=lambda kv: -kv[1])[:10]
    lines = [
        "",
        "#" * 72,
        "# RUN SUMMARY",
        "#" * 72,
        f"Scanned: {scanned}",
        f"Saved:   {kept}",
        f"Skipped ads: {skipped_ads}",
        f"Transcript sources: "
        + ", ".join(f"{k}={v}" for k, v in transcript_counts.items())
        if transcript_counts
        else "Transcript sources: (none)",
        "Top channels:",
    ]
    if top_channels:
        for ch, n in top_channels:
            lines.append(f"  {n:3d}  {ch}")
    else:
        lines.append("  (none)")
    lines.append("")
    with data_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def check_ffmpeg_available() -> bool:
    import shutil

    return shutil.which("ffmpeg") is not None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def scrape(
    max_videos: int,
    output_dir: Path,
    headless: bool,
    min_views: int,
    whisper_model: str | None,
) -> None:
    if yt_dlp is None:
        log.warning(
            "yt-dlp is not installed — view counts will rely on fallbacks only. "
            "Install with: pip install yt-dlp"
        )
    if whisper_model and not check_ffmpeg_available():
        log.warning(
            "ffmpeg was not found on PATH. Whisper needs it to decode audio; "
            "transcripts will fall back to '(none)' for Shorts without captions."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    data_path = output_dir / "data.txt"
    jsonl_path = output_dir / "shorts.jsonl"
    ads_path = output_dir / "ads_filtered.jsonl"

    machine_meta = machine_identity.current(
        config={
            "max_videos": max_videos,
            "min_views": min_views,
            "whisper_model": whisper_model,
            "output_dir": str(output_dir),
        }
    )
    log.info(
        "machine_id=%s host=%s scraper=%s config=%s",
        machine_meta.machine_id,
        machine_meta.hostname,
        machine_meta.scraper_version,
        machine_meta.scraper_config_hash,
    )

    store = MetadataStore()
    resumed_ids = load_previous_ids(jsonl_path)
    if resumed_ids:
        log.info("Resume: %d Shorts already recorded, will skip them.", len(resumed_ids))
    seen_ids: set[str] = set(resumed_ids)
    kept = 0
    scanned = 0
    skipped_ads = 0
    empty_streak = 0
    channel_counts: dict[str, int] = {}
    transcript_counts: dict[str, int] = {}

    def _setup_context(ctx: BrowserContext) -> None:
        # Every new incognito context needs its own player-response listener.
        install_player_interceptor(ctx, store)

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--incognito"],
        )
        sessions = SessionManager(
            browser,
            output_dir=output_dir,
            context_setup=_setup_context,
        )

        batch_writer: BatchWriter | None = None
        current_batch_session: str | None = None

        def _swap_batch_writer(session_obj) -> BatchWriter:
            """Close the previous batch file (if any) and open one for the current session."""
            nonlocal batch_writer, current_batch_session
            if batch_writer is not None:
                try:
                    batch_writer.close(
                        footer_extras={
                            "ads_filtered": session_obj.ads_filtered,
                        }
                    )
                except Exception as exc:
                    log.warning("batch_writer.close failed: %s", exc)
            envelope = BatchEnvelope(
                machine_id=machine_meta.machine_id,
                hostname=machine_meta.hostname,
                scraper_version=machine_meta.scraper_version,
                scraper_config_hash=machine_meta.scraper_config_hash,
                session_id=session_obj.id,
                session_started_at=session_obj.created_at.isoformat(),
                user_agent=session_obj.user_agent,
                viewport=f"{session_obj.viewport['width']}x{session_obj.viewport['height']}",
            )
            batch_writer = BatchWriter(output_dir, envelope)
            current_batch_session = session_obj.id
            return batch_writer

        last_id: str | None = None
        try:
            while scanned < max_videos:
                page = sessions.page()  # rotates silently when the session ages out
                page.wait_for_timeout(800)

                video_id = current_video_id(page)
                if not video_id:
                    empty_streak += 1
                    if empty_streak > 5:
                        log.warning("Too many empty reads, stopping")
                        break
                    last_id = advance(page, last_id)
                    continue
                empty_streak = 0

                # Resolve metadata before the ad check so adPlacements/isAd
                # flags from the player response feed into the verdict.
                time.sleep(0.5)
                meta = resolve_metadata(context_for_session(sessions), store, video_id)

                verdict = ad_filter.classify(page, meta)
                if verdict.is_ad:
                    skipped_ads += 1
                    sessions.record_ad()
                    _log_ad_drop(ads_path, video_id, sessions.current_id, verdict)
                    last_id = advance(page, video_id)
                    continue

                if video_id in seen_ids:
                    last_id = advance(page, video_id)
                    continue
                seen_ids.add(video_id)
                scanned += 1

                views = int(meta.get("view_count") or 0)
                title = meta.get("title") or ""
                channel = meta.get("channel") or ""
                description = meta.get("description") or ""
                keywords = meta.get("keywords") or []
                duration = meta.get("duration_seconds")
                source = meta.get("source") or "none"

                log.info(
                    "[%d] %s | %s | %s views (src=%s)",
                    scanned,
                    (channel or "?")[:30],
                    (title or "?")[:50],
                    f"{views:,}",
                    source,
                )

                if views < min_views:
                    last_id = advance(page, video_id)
                    continue

                transcript, hook, t_source = fetch_transcript(video_id, whisper_model)
                hashtags = extract_hashtags(title, description)
                for kw in keywords:
                    if kw.startswith("#") and kw not in hashtags:
                        hashtags.append(kw)

                session = sessions.current
                record = ShortRecord(
                    video_id=video_id,
                    url=f"https://www.youtube.com/shorts/{video_id}",
                    channel=channel,
                    title=title,
                    description=description,
                    hashtags=hashtags,
                    keywords=list(keywords),
                    view_count=views,
                    duration_seconds=duration,
                    transcript=transcript,
                    hook=hook,
                    metadata_source=source,
                    transcript_source=t_source,
                    has_paid_promotion_disclosure=verdict.has_paid_promotion_disclosure,
                    session_id=session.id if session else "",
                    user_agent=session.user_agent if session else "",
                    viewport=(
                        f"{session.viewport['width']}x{session.viewport['height']}"
                        if session
                        else ""
                    ),
                )
                append_record(data_path, jsonl_path, record)
                # Distributed batch emission: swap writers on session rotation,
                # write one observation per kept record. Transcript + hook ride
                # along so the ingestor can populate video_analyses on first
                # sight, even though enrichment will still fill in embeddings.
                if session is not None and session.id != current_batch_session:
                    _swap_batch_writer(session)
                if batch_writer is not None:
                    batch_writer.write_observation(
                        {
                            "source_video_id": record.video_id,
                            "url": record.url,
                            "author_handle": record.channel or None,
                            "title": record.title or None,
                            "description": record.description or None,
                            "hashtags": record.hashtags or [],
                            "keywords": record.keywords or [],
                            "duration_seconds": record.duration_seconds,
                            "view_count": record.view_count,
                            "is_ad": False,
                            "has_paid_promotion_disclosure": record.has_paid_promotion_disclosure,
                            "transcript": record.transcript,
                            "hook": record.hook,
                            "metadata_source": record.metadata_source,
                            "transcript_source": record.transcript_source,
                        }
                    )
                kept += 1
                sessions.record_kept(channel)
                if channel:
                    channel_counts[channel] = channel_counts.get(channel, 0) + 1
                transcript_counts[t_source] = transcript_counts.get(t_source, 0) + 1
                log.info(
                    "  -> saved (%d total >= %d views, transcript=%s)",
                    kept,
                    min_views,
                    t_source,
                )

                last_id = advance(page, video_id)
        finally:
            if batch_writer is not None:
                try:
                    footer_extras = {}
                    if sessions.current is not None:
                        footer_extras["ads_filtered"] = sessions.current.ads_filtered
                    batch_writer.close(footer_extras=footer_extras)
                except Exception as exc:
                    log.warning("batch_writer.close failed on shutdown: %s", exc)
            sessions.close()
            browser.close()

    append_summary(
        data_path, scanned, kept, skipped_ads, channel_counts, transcript_counts
    )
    stats_path = output_dir / "stats.txt"
    n_total = stats_module.write_report(jsonl_path, stats_path)
    log.info(
        "Done. Scanned %d, kept %d, ads skipped %d. Output: %s (stats: %s, %d rows)",
        scanned,
        kept,
        skipped_ads,
        data_path,
        stats_path,
        n_total,
    )


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape popular YouTube Shorts.")
    parser.add_argument("--max-videos", type=int, default=500)
    parser.add_argument("--min-views", type=int, default=MIN_VIEWS)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--whisper-model",
        default="tiny",
        help="faster-whisper model to use when captions are missing "
        "(tiny/base/small/medium). Default: tiny.",
    )
    parser.add_argument(
        "--no-whisper",
        dest="whisper_enabled",
        action="store_false",
        help="Disable Whisper fallback; use captions only.",
    )
    parser.set_defaults(whisper_enabled=True)
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="Skip scraping; recompute stats.txt from existing shorts.jsonl.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.stats_only:
        jsonl_path = args.output_dir / "shorts.jsonl"
        stats_path = args.output_dir / "stats.txt"
        n = stats_module.write_report(jsonl_path, stats_path)
        log.info("Wrote %s (%d records)", stats_path, n)
        return 0

    whisper_model = args.whisper_model if args.whisper_enabled else None
    try:
        scrape(
            max_videos=args.max_videos,
            output_dir=args.output_dir,
            headless=args.headless,
            min_views=args.min_views,
            whisper_model=whisper_model,
        )
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
