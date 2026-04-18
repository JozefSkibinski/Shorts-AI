"""YouTube Shorts scraper.

Opens YouTube Shorts in an incognito Playwright context so browsing does not
influence the logged-in algorithm, auto-scrolls through videos, and records
metadata for every Short with more than 1,000,000 views.

View count, title, author, length and description come from the internal
``/youtubei/v1/player`` responses that YouTube fires for each Short as it
loads, which is far more reliable than scraping the rendered DOM.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
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
    """Return (full_transcript, hook) or (None, None) if unavailable."""
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
# Network interception
# ---------------------------------------------------------------------------


class MetadataStore:
    """Thread-safe map of video_id -> metadata dict captured from /player."""

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self._lock = Lock()

    def update(self, video_id: str, fields: dict) -> None:
        with self._lock:
            existing = self._data.setdefault(video_id, {})
            for k, v in fields.items():
                if v is not None and v != "":
                    existing[k] = v

    def get(self, video_id: str) -> dict | None:
        with self._lock:
            return dict(self._data.get(video_id) or {}) or None


def _parse_player_payload(payload: dict) -> tuple[str | None, dict]:
    details = payload.get("videoDetails") or {}
    vid = details.get("videoId")
    if not vid:
        return None, {}

    try:
        views = int(details.get("viewCount") or 0)
    except (TypeError, ValueError):
        views = 0
    try:
        duration = float(details.get("lengthSeconds") or 0) or None
    except (TypeError, ValueError):
        duration = None

    keywords = details.get("keywords") or []
    fields = {
        "title": (details.get("title") or "").strip(),
        "channel": (details.get("author") or "").strip(),
        "description": (details.get("shortDescription") or "").strip(),
        "view_count": views,
        "duration_seconds": duration,
        "keywords": list(keywords),
    }
    return vid, fields


def install_player_interceptor(context: BrowserContext, store: MetadataStore) -> None:
    def handle(response: Response) -> None:
        url = response.url
        if "/youtubei/v1/player" not in url:
            return
        try:
            data = response.json()
        except Exception:
            return
        vid, fields = _parse_player_payload(data)
        if vid and fields:
            store.update(vid, fields)
            log.debug("captured %s: %s views", vid, fields.get("view_count"))

    context.on("response", handle)


# ---------------------------------------------------------------------------
# HTTP fallback: fetch the Short's page and parse ytInitialPlayerResponse
# ---------------------------------------------------------------------------


def _balanced_json(text: str, start: int) -> str | None:
    """Return the JSON object starting at index `start` (must be a '{')."""
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


def parse_initial_player_response(html: str) -> dict | None:
    marker = "ytInitialPlayerResponse"
    idx = html.find(marker)
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


def fetch_metadata_http(context: BrowserContext, video_id: str) -> dict | None:
    """Fetch the Short's HTML page via the incognito context and parse metadata."""
    url = f"https://www.youtube.com/shorts/{video_id}"
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
            log.debug("HTTP %s for %s", resp.status, video_id)
            return None
        html = resp.text()
    except Exception as exc:
        log.debug("HTTP fetch failed for %s: %s", video_id, exc)
        return None

    payload = parse_initial_player_response(html)
    if not payload:
        log.debug("no ytInitialPlayerResponse for %s", video_id)
        return None
    _, fields = _parse_player_payload(payload)
    return fields or None


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
    """Read the currently visible Short's video id from the URL or DOM."""
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
                const id = a.getAttribute('video-id') || a.id;
                return id || null;
            }"""
        )
    except Exception:
        vid = None
    return vid


def wait_for_metadata(
    context: BrowserContext,
    store: MetadataStore,
    video_id: str,
    timeout_ms: int = 4000,
) -> dict | None:
    """Return metadata for video_id.

    Waits briefly for the /player response interceptor to populate the store,
    then falls back to fetching the Short's HTML page over HTTP and parsing
    ``ytInitialPlayerResponse``. The HTTP path is the reliable one — the
    interceptor is just an optimisation when it happens to fire.
    """
    import time as _t

    waited = 0
    step = 250
    while waited < timeout_ms:
        meta = store.get(video_id)
        if meta and meta.get("view_count"):
            return meta
        _t.sleep(step / 1000.0)
        waited += step

    fields = fetch_metadata_http(context, video_id)
    if fields:
        store.update(video_id, fields)
        return store.get(video_id)
    return store.get(video_id)


def advance(page: Page) -> None:
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(random.randint(1500, 2800))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "video_id",
    "url",
    "channel",
    "title",
    "view_count",
    "duration_seconds",
    "hashtags",
    "description",
    "hook",
    "transcript",
]


def append_row(csv_path: Path, jsonl_path: Path, record: ShortRecord) -> None:
    is_new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        row = asdict(record)
        row["hashtags"] = " ".join(record.hashtags)
        writer.writerow(row)

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
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "shorts.csv"
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
        log.info("Opening %s", SHORTS_URL)
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

            meta = wait_for_metadata(context, store, video_id, timeout_ms=4000) or {}
            views = int(meta.get("view_count") or 0)
            title = meta.get("title") or ""
            channel = meta.get("channel") or ""
            description = meta.get("description") or ""
            keywords = meta.get("keywords") or []
            duration = meta.get("duration_seconds")

            log.info(
                "[%d] %s | %s | %s views",
                scanned,
                channel or "?",
                title[:60] or "?",
                f"{views:,}",
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
            )
            append_row(csv_path, jsonl_path, record)
            kept += 1
            log.info("  -> kept (%d total >= %d views)", kept, min_views)

            advance(page)

        browser.close()

    log.info("Done. Scanned %d, kept %d. Output: %s", scanned, kept, output_dir)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape popular YouTube Shorts.")
    parser.add_argument(
        "--max-videos",
        type=int,
        default=500,
        help="How many unique Shorts to scan before stopping (default 500).",
    )
    parser.add_argument(
        "--min-views",
        type=int,
        default=MIN_VIEWS,
        help="Only log Shorts with at least this many views (default 1,000,000).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Directory to write shorts.csv / shorts.jsonl (default ./output).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium without a visible window.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging."
    )
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
