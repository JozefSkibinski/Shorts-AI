"""Browser-context rotation for the YouTube Shorts scraper.

Every 50–70 minutes the current incognito context is closed (with its
cookies, storage, IndexedDB, service workers) and a fresh one spawned with
a new user-agent + viewport. A single JSONL line per session is appended
to ``output/sessions.jsonl`` for audit + the bridge.

Sync-only to match the rest of the scraper. The caller asks for
``sessions.page()`` in its scroll loop; rotation happens lazily behind
that call whenever the current session has expired.
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)


log = logging.getLogger(__name__)


# Kept deliberately short and current. Refresh quarterly.
USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

VIEWPORTS: tuple[dict[str, int], ...] = (
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
)

# Lifetime jitter bounds (minutes). Spec: ~60m with ±10m jitter.
ROTATION_INTERVAL_MIN = 50
ROTATION_INTERVAL_MAX = 70

YOUTUBE_HOME = "https://www.youtube.com/"
SHORTS_URL = "https://www.youtube.com/shorts"

# Requests that never matter to the scrape — aborting them speeds up the
# warm-up and reduces the session's noise footprint.
_BLOCKED_HOSTS: tuple[str, ...] = (
    "doubleclick.net",
    "googleadservices",
    "google-analytics",
    "googletagmanager.com",
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SessionStats:
    session_id: str
    created_at: str
    closed_at: str | None = None
    user_agent: str = ""
    viewport: str = ""
    videos_scraped: int = 0
    ads_filtered: int = 0
    unique_channels_seen: int = 0
    channels_distribution: dict[str, int] = field(default_factory=dict)

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class _Session:
    id: str
    created_at: datetime
    expires_at: datetime
    user_agent: str
    viewport: dict[str, int]
    context: BrowserContext
    page: Page
    videos_scraped: int = 0
    ads_filtered: int = 0
    channel_counter: Counter = field(default_factory=Counter)

    def record_kept(self, channel: str) -> None:
        self.videos_scraped += 1
        if channel:
            self.channel_counter[channel] += 1

    def record_ad(self) -> None:
        self.ads_filtered += 1

    def to_stats(self) -> SessionStats:
        return SessionStats(
            session_id=self.id,
            created_at=self.created_at.isoformat(),
            closed_at=datetime.now(timezone.utc).isoformat(),
            user_agent=self.user_agent,
            viewport=f"{self.viewport['width']}x{self.viewport['height']}",
            videos_scraped=self.videos_scraped,
            ads_filtered=self.ads_filtered,
            unique_channels_seen=len(self.channel_counter),
            channels_distribution=dict(self.channel_counter),
        )


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


ContextSetupFn = Callable[[BrowserContext], None]


class SessionManager:
    """Owns the incognito browser context + scraping page.

    The scraper calls ``manager.page()`` each iteration. That call is free
    while the current session is valid and lazily rotates the context when
    the session has aged out.
    """

    def __init__(
        self,
        browser: Browser,
        output_dir: Path,
        *,
        context_setup: ContextSetupFn | None = None,
        rotation_min: int = ROTATION_INTERVAL_MIN,
        rotation_max: int = ROTATION_INTERVAL_MAX,
        block_tracking: bool = True,
    ) -> None:
        self.browser = browser
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_path = self.output_dir / "sessions.jsonl"
        self._context_setup = context_setup
        self._rotation_min = rotation_min
        self._rotation_max = rotation_max
        self._block_tracking = block_tracking
        self._current: _Session | None = None

    # ----- public API -------------------------------------------------------

    @property
    def current_id(self) -> str | None:
        return self._current.id if self._current else None

    @property
    def current(self) -> _Session | None:
        return self._current

    def expired(self) -> bool:
        s = self._current
        return s is None or datetime.now(timezone.utc) >= s.expires_at

    def page(self) -> Page:
        """Return a live Page, rotating if the current session has aged out."""
        if self.expired():
            self._rotate()
        assert self._current is not None
        return self._current.page

    def record_kept(self, channel: str) -> None:
        if self._current:
            self._current.record_kept(channel)

    def record_ad(self) -> None:
        if self._current:
            self._current.record_ad()

    def close(self) -> None:
        """Finalize current session. Called by the scraper on graceful exit."""
        if self._current is not None:
            self._finalize(self._current)
            self._current = None

    # ----- rotation internals ----------------------------------------------

    def _rotate(self) -> None:
        if self._current is not None:
            self._finalize(self._current)

        ua = random.choice(USER_AGENTS)
        vp = random.choice(VIEWPORTS)
        lifetime = random.randint(self._rotation_min, self._rotation_max)
        created = datetime.now(timezone.utc)

        context = self.browser.new_context(
            user_agent=ua,
            viewport=vp,
            locale="en-US",
            java_script_enabled=True,
        )
        context.clear_cookies()

        if self._block_tracking:
            context.route("**/*", _route_filter)

        if self._context_setup:
            try:
                self._context_setup(context)
            except Exception as exc:
                log.warning("context_setup hook failed: %s", exc)

        page = context.new_page()
        self._warm(page)

        session = _Session(
            id=str(uuid.uuid4()),
            created_at=created,
            expires_at=created + timedelta(minutes=lifetime),
            user_agent=ua,
            viewport=vp,
            context=context,
            page=page,
        )
        log.info(
            "new session %s (ua=%s... viewport=%dx%d lifetime=%dmin)",
            session.id[:8],
            ua.split()[0],
            vp["width"],
            vp["height"],
            lifetime,
        )
        self._current = session

    def _warm(self, page: Page) -> None:
        """Open youtube.com, dismiss consent, then navigate to the Shorts feed."""
        try:
            page.goto(YOUTUBE_HOME, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeoutError:
            log.warning("warm-up: youtube.com timed out")

        for sel in (
            'button[aria-label*="Accept all"]',
            'button[aria-label*="Reject all"]',
            'button:has-text("Accept all")',
            'button:has-text("I agree")',
        ):
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=800):
                    btn.click(timeout=2000)
                    page.wait_for_timeout(500)
                    break
            except Exception:
                continue

        try:
            page.goto(SHORTS_URL, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeoutError:
            log.warning("warm-up: shorts feed timed out")

        try:
            page.wait_for_selector("ytd-reel-video-renderer", timeout=15_000)
        except PlaywrightTimeoutError:
            pass

    def _finalize(self, session: _Session) -> None:
        stats = session.to_stats()
        try:
            with self.sessions_path.open("a", encoding="utf-8") as f:
                f.write(stats.to_json_line() + "\n")
        except Exception as exc:
            log.warning("Could not write session row: %s", exc)
        try:
            session.page.close()
        except Exception:
            pass
        try:
            session.context.close()
        except Exception:
            pass
        log.info(
            "closed session %s: %d kept, %d ads, %d channels",
            session.id[:8],
            session.videos_scraped,
            session.ads_filtered,
            len(session.channel_counter),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _route_filter(route: Any) -> None:
    try:
        url = route.request.url
    except Exception:
        route.continue_()
        return
    if any(h in url for h in _BLOCKED_HOSTS):
        try:
            route.abort()
            return
        except Exception:
            pass
    try:
        route.continue_()
    except Exception:
        pass
