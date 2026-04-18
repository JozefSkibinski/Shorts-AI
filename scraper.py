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
    view_count: int = 0
    duration_seconds: float | None = None
    transcript: str | None = None
    hook: str | None = None
    metadata_source: str = ""


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


def fetch_transcript(video_id: str) -> tuple[str | None, str | None]:
    try:
        snippets = YouTubeTranscriptApi.get_transcript(
            video_id, languages=["en", "en-US", "en-GB"]
        )
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable):
        return None, None
    except Exception as exc:
        log.debug("Transcript failed for %s: %s", video_id, exc)
        return None, None

    full = " ".join(s["text"].strip() for s in snippets if s.get("text"))
    hook_parts = [
        s["text"].strip()
        for s in snippets
        if s.get("text") and s.get("start", 0) < HOOK_SECONDS
    ]
    hook = " ".join(hook_parts) if hook_parts else None
    return full.strip() or None, hook


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
    return {
        "title": (details.get("title") or "").strip(),
        "channel": (details.get("author") or "").strip(),
        "description": (details.get("shortDescription") or "").strip(),
        "view_count": views,
        "duration_seconds": duration,
        "keywords": list(details.get("keywords") or []),
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


def advance(page: Page) -> None:
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(random.randint(1500, 2800))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "unknown"
    s = int(round(seconds))
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"


def format_record(record: ShortRecord) -> str:
    hashtags = " ".join(record.hashtags) if record.hashtags else "(none)"
    hook = record.hook or "(no captions)"
    transcript = record.transcript or "(no captions)"
    description = record.description.strip() or "(empty)"

    return (
        "=" * 72 + "\n"
        f"Video: {record.title or '(no title)'}\n"
        f"Channel: {record.channel or '(unknown)'}\n"
        f"Views: {record.view_count:,}\n"
        f"Length: {format_duration(record.duration_seconds)}\n"
        f"Link: {record.url}\n"
        f"Hashtags: {hashtags}\n"
        f"Source: {record.metadata_source or '(unknown)'}\n"
        "--- Description ---\n"
        f"{description}\n"
        "--- Hook (first 7s) ---\n"
        f"{hook}\n"
        "--- Transcript ---\n"
        f"{transcript}\n"
    )


def append_record(data_path: Path, jsonl_path: Path, record: ShortRecord) -> None:
    with data_path.open("a", encoding="utf-8") as f:
        f.write(format_record(record))
        f.write("\n")
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def scrape(
    max_videos: int,
    output_dir: Path,
    headless: bool,
    min_views: int,
) -> None:
    if yt_dlp is None:
        log.warning(
            "yt-dlp is not installed — view counts will rely on fallbacks only. "
            "Install with: pip install yt-dlp"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    data_path = output_dir / "data.txt"
    jsonl_path = output_dir / "shorts.jsonl"

    store = MetadataStore()
    seen_ids: set[str] = set()
    kept = 0
    scanned = 0
    empty_streak = 0

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--incognito"],
        )
        context: BrowserContext = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 430, "height": 900},
            locale="en-US",
            java_script_enabled=True,
        )
        context.clear_cookies()
        install_player_interceptor(context, store)

        page = context.new_page()
        log.info("Opening %s (incognito)", SHORTS_URL)
        page.goto(SHORTS_URL, wait_until="domcontentloaded", timeout=60_000)
        dismiss_consent(page)

        try:
            page.wait_for_selector("ytd-reel-video-renderer", timeout=30_000)
        except PlaywrightTimeoutError:
            log.error("Shorts feed never loaded")
            browser.close()
            return

        page.wait_for_timeout(2500)

        while scanned < max_videos:
            page.wait_for_timeout(800)
            video_id = current_video_id(page)
            if not video_id:
                empty_streak += 1
                if empty_streak > 5:
                    log.warning("Too many empty reads, stopping")
                    break
                advance(page)
                continue
            empty_streak = 0

            if video_id in seen_ids:
                advance(page)
                continue
            seen_ids.add(video_id)
            scanned += 1

            # Give the interceptor a moment to catch this video's /player call
            time.sleep(0.5)
            meta = resolve_metadata(context, store, video_id)

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
                advance(page)
                continue

            transcript, hook = fetch_transcript(video_id)
            hashtags = extract_hashtags(title, description)
            for kw in keywords:
                if kw.startswith("#") and kw not in hashtags:
                    hashtags.append(kw)

            record = ShortRecord(
                video_id=video_id,
                url=f"https://www.youtube.com/shorts/{video_id}",
                channel=channel,
                title=title,
                description=description,
                hashtags=hashtags,
                view_count=views,
                duration_seconds=duration,
                transcript=transcript,
                hook=hook,
                metadata_source=source,
            )
            append_record(data_path, jsonl_path, record)
            kept += 1
            log.info("  -> saved (%d total >= %d views)", kept, min_views)

            advance(page)

        browser.close()

    log.info("Done. Scanned %d, kept %d. Output: %s", scanned, kept, data_path)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape popular YouTube Shorts.")
    parser.add_argument("--max-videos", type=int, default=500)
    parser.add_argument("--min-views", type=int, default=MIN_VIEWS)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        scrape(
            max_videos=args.max_videos,
            output_dir=args.output_dir,
            headless=args.headless,
            min_views=args.min_views,
        )
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
