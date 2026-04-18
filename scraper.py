"""YouTube Shorts scraper.

Opens YouTube Shorts in an incognito Playwright context so browsing does not
influence the logged-in algorithm, auto-scrolls through videos, and records
metadata for every Short with more than 1,000,000 views.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
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

VIEW_SUFFIXES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_view_count(raw: str | None) -> int:
    """Convert '1.2M views' / '523K' / '1,234,567 views' into an int."""
    if not raw:
        return 0
    text = raw.strip().lower().replace("views", "").replace("view", "").strip()
    text = text.replace(",", "")
    if not text:
        return 0
    match = re.match(r"([\d.]+)\s*([kmb]?)", text)
    if not match:
        return 0
    number = float(match.group(1))
    suffix = match.group(2)
    return int(number * VIEW_SUFFIXES.get(suffix, 1))


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
    except Exception as exc:  # network / parser errors
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
# Page interaction
# ---------------------------------------------------------------------------


def dismiss_consent(page: Page) -> None:
    """Best-effort dismissal of the EU / cookie consent wall."""
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


def current_short_data(page: Page) -> dict | None:
    """Read DOM fields for the currently visible Short."""
    js = """
    () => {
        const active = document.querySelector('ytd-reel-video-renderer[is-active]')
            || document.querySelector('ytd-shorts ytd-reel-video-renderer');
        if (!active) return null;

        const text = (sel) => {
            const el = active.querySelector(sel);
            return el ? (el.innerText || el.textContent || '').trim() : '';
        };

        const title = text('yt-shorts-video-title-view-model')
            || text('h2.title')
            || text('#title');

        const channel = text('ytd-channel-name a')
            || text('#channel-name a')
            || text('a.yt-simple-endpoint[href*="/@"]');

        // Description / caption
        let description = text('#description')
            || text('#caption')
            || text('yt-shorts-video-description-view-model');

        // View count appears in several layouts
        let views = '';
        const viewEl = active.querySelector(
            '[aria-label*="views" i], [aria-label*="View count"], #view-count, .view-count'
        );
        if (viewEl) {
            views = viewEl.getAttribute('aria-label') || viewEl.innerText || '';
        }

        // Video element for duration
        const videoEl = active.querySelector('video');
        const duration = videoEl && !isNaN(videoEl.duration) ? videoEl.duration : null;

        return {
            title,
            channel,
            description,
            views_text: views,
            duration,
            href: location.href,
        };
    }
    """
    try:
        return page.evaluate(js)
    except Exception as exc:
        log.debug("DOM read failed: %s", exc)
        return None


def advance(page: Page) -> None:
    """Move to the next Short. 'j' works when no input is focused."""
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

    seen_ids: set[str] = set()
    kept = 0
    scanned = 0
    empty_streak = 0

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--incognito"],
        )
        # A fresh context with no storage_state is effectively incognito; combined
        # with the --incognito flag above it guarantees no profile data is reused.
        context: BrowserContext = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 430, "height": 900},
            locale="en-US",
            java_script_enabled=True,
        )
        context.clear_cookies()

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
            page.wait_for_timeout(1200)
            data = current_short_data(page)
            if not data or not data.get("href"):
                empty_streak += 1
                if empty_streak > 5:
                    log.warning("Too many empty reads, stopping")
                    break
                advance(page)
                continue
            empty_streak = 0

            video_id = extract_video_id(data["href"])
            if not video_id or video_id in seen_ids:
                advance(page)
                continue
            seen_ids.add(video_id)
            scanned += 1

            views = parse_view_count(data.get("views_text"))
            title = (data.get("title") or "").strip()
            channel = (data.get("channel") or "").strip()
            description = (data.get("description") or "").strip()

            log.info(
                "[%d] %s | %s | %s views", scanned, channel or "?", title[:60], f"{views:,}"
            )

            if views < min_views:
                advance(page)
                continue

            transcript, hook = fetch_transcript(video_id)
            record = ShortRecord(
                video_id=video_id,
                url=f"https://www.youtube.com/shorts/{video_id}",
                channel=channel,
                title=title,
                description=description,
                hashtags=extract_hashtags(title, description),
                view_count=views,
                duration_seconds=data.get("duration"),
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
