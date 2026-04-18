"""SessionManager — rotation timing, stats emission, cleanup."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from session_manager import (
    ROTATION_INTERVAL_MAX,
    ROTATION_INTERVAL_MIN,
    SessionManager,
    USER_AGENTS,
    VIEWPORTS,
    _Session,
    SessionStats,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakePage:
    def __init__(self):
        self.closed = False
        self.gotos: list[str] = []

    def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append(url)

    def wait_for_selector(self, sel, timeout=None):
        return None

    def wait_for_timeout(self, ms):
        return None

    def locator(self, sel):
        class _L:
            @property
            def first(self):
                return self

            def is_visible(self, timeout=None):
                return False

            def click(self, timeout=None):
                return None

        return _L()

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.pages: list[FakePage] = []
        self.closed = False
        self.cleared_cookies = False
        self.routes: list[str] = []

    def new_page(self):
        p = FakePage()
        self.pages.append(p)
        return p

    def clear_cookies(self):
        self.cleared_cookies = True

    def route(self, pattern, handler):
        self.routes.append(pattern)

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts: list[FakeContext] = []
        self.new_context_calls: list[dict] = []

    def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_first_page_call_spawns_a_session(tmp_path: Path):
    b = FakeBrowser()
    mgr = SessionManager(b, output_dir=tmp_path)
    page = mgr.page()
    assert isinstance(page, FakePage)
    assert mgr.current is not None
    assert mgr.current_id == mgr.current.id
    assert len(b.contexts) == 1
    assert b.contexts[0].cleared_cookies is True


def test_new_context_uses_pooled_ua_and_viewport(tmp_path: Path):
    b = FakeBrowser()
    mgr = SessionManager(b, output_dir=tmp_path)
    mgr.page()
    call = b.new_context_calls[-1]
    assert call["user_agent"] in USER_AGENTS
    assert call["viewport"] in VIEWPORTS
    assert call["locale"] == "en-US"


def test_second_call_before_expiry_returns_same_page(tmp_path: Path):
    b = FakeBrowser()
    mgr = SessionManager(b, output_dir=tmp_path)
    page1 = mgr.page()
    page2 = mgr.page()
    assert page1 is page2
    assert len(b.contexts) == 1


def test_rotation_after_expiry_writes_session_row(tmp_path: Path):
    b = FakeBrowser()
    mgr = SessionManager(b, output_dir=tmp_path)
    page1 = mgr.page()
    mgr.record_kept("ChannelA")
    mgr.record_kept("ChannelA")
    mgr.record_kept("ChannelB")
    mgr.record_ad()

    # Force the current session to be expired
    mgr.current.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert mgr.expired() is True

    page2 = mgr.page()
    assert page2 is not page1
    assert len(b.contexts) == 2
    assert b.contexts[0].closed is True  # first context cleaned up

    sessions_file = tmp_path / "sessions.jsonl"
    assert sessions_file.exists()
    rows = [json.loads(l) for l in sessions_file.read_text().splitlines() if l]
    assert len(rows) == 1
    row = rows[0]
    assert row["videos_scraped"] == 3
    assert row["ads_filtered"] == 1
    assert row["unique_channels_seen"] == 2
    assert row["channels_distribution"] == {"ChannelA": 2, "ChannelB": 1}
    assert "closed_at" in row and row["closed_at"]
    assert "session_id" in row


def test_close_finalizes_the_last_session(tmp_path: Path):
    b = FakeBrowser()
    mgr = SessionManager(b, output_dir=tmp_path)
    mgr.page()
    mgr.record_kept("Ch")
    mgr.close()

    assert mgr.current is None
    rows = [
        json.loads(l)
        for l in (tmp_path / "sessions.jsonl").read_text().splitlines()
        if l
    ]
    assert len(rows) == 1
    assert rows[0]["videos_scraped"] == 1


def test_rotation_lifetime_within_configured_bounds(tmp_path: Path):
    b = FakeBrowser()
    mgr = SessionManager(
        b,
        output_dir=tmp_path,
        rotation_min=5,
        rotation_max=7,
    )
    for _ in range(20):
        mgr.page()
        elapsed_min = (mgr.current.expires_at - mgr.current.created_at).total_seconds() / 60
        assert 5 <= elapsed_min <= 7
        # Force immediate rotation for the next loop iter
        mgr.current.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    mgr.close()


def test_default_bounds_are_sane():
    assert ROTATION_INTERVAL_MIN < ROTATION_INTERVAL_MAX
    assert ROTATION_INTERVAL_MIN >= 30  # don't rotate faster than cold-start baseline
    assert ROTATION_INTERVAL_MAX <= 120


def test_session_stats_serializes_cleanly():
    stats = SessionStats(
        session_id="s",
        created_at="2026-04-18T00:00:00+00:00",
        user_agent="UA",
        viewport="1920x1080",
        videos_scraped=5,
        ads_filtered=2,
        unique_channels_seen=3,
        channels_distribution={"A": 3, "B": 2},
    )
    parsed = json.loads(stats.to_json_line())
    assert parsed["session_id"] == "s"
    assert parsed["channels_distribution"] == {"A": 3, "B": 2}
