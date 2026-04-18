"""Bridge unit tests — parsing + jsonl tailing, no DB required."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from app.scraper_bridge import AdDropRow, ScraperRow, SessionRow, iter_jsonl


def test_scraper_row_from_dict_minimal():
    row = ScraperRow.from_dict({
        "video_id": "abc",
        "url": "https://www.youtube.com/shorts/abc",
        "view_count": "1234567",
        "duration_seconds": "42.5",
    })
    assert row is not None
    assert row.video_id == "abc"
    assert row.view_count == 1234567
    assert row.duration_seconds == 42.5
    assert row.hashtags == []
    assert row.keywords == []


def test_scraper_row_from_dict_rejects_missing_keys():
    assert ScraperRow.from_dict({"url": "https://x"}) is None
    assert ScraperRow.from_dict({"video_id": "x"}) is None


def test_scraper_row_handles_garbage_numbers():
    row = ScraperRow.from_dict({
        "video_id": "x", "url": "u",
        "view_count": "not-a-number", "duration_seconds": "also-not",
    })
    assert row.view_count == 0
    assert row.duration_seconds is None


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_iter_jsonl_yields_offsets_and_payloads(tmp_path: Path):
    p = tmp_path / "shorts.jsonl"
    _write_jsonl(p, [{"video_id": "a"}, {"video_id": "b"}])
    out = list(iter_jsonl(p, 0))
    assert len(out) == 2
    assert out[0][1]["video_id"] == "a"
    assert out[1][1]["video_id"] == "b"
    assert out[0][0] < out[1][0]


def test_iter_jsonl_resumes_from_offset(tmp_path: Path):
    p = tmp_path / "shorts.jsonl"
    _write_jsonl(p, [{"video_id": "a"}, {"video_id": "b"}])
    first = list(iter_jsonl(p, 0))
    second = list(iter_jsonl(p, first[0][0]))
    assert [payload["video_id"] for _, payload in second] == ["b"]


def test_iter_jsonl_skips_malformed_lines(tmp_path: Path, caplog):
    p = tmp_path / "shorts.jsonl"
    p.write_text(
        '{"video_id": "good"}\n'
        "this is not json\n"
        '{"video_id": "alsoGood"}\n',
        encoding="utf-8",
    )
    out = [payload["video_id"] for _, payload in iter_jsonl(p, 0)]
    assert out == ["good", "alsoGood"]


def test_iter_jsonl_missing_file_returns_empty(tmp_path: Path):
    assert list(iter_jsonl(tmp_path / "nope.jsonl", 0)) == []


# ---------------------------------------------------------------------------
# Scraper hygiene fields (is_ad, session_id, disclosure)
# ---------------------------------------------------------------------------


def test_scraper_row_propagates_hygiene_fields():
    sid = str(uuid4())
    row = ScraperRow.from_dict({
        "video_id": "abc",
        "url": "https://www.youtube.com/shorts/abc",
        "has_paid_promotion_disclosure": True,
        "session_id": sid,
    })
    assert row.has_paid_promotion_disclosure is True
    assert str(row.session_id) == sid


def test_scraper_row_defaults_hygiene_fields_when_missing():
    row = ScraperRow.from_dict({
        "video_id": "abc",
        "url": "https://www.youtube.com/shorts/abc",
    })
    assert row.has_paid_promotion_disclosure is False
    assert row.session_id is None


def test_scraper_row_rejects_bad_session_uuid():
    row = ScraperRow.from_dict({
        "video_id": "abc",
        "url": "https://www.youtube.com/shorts/abc",
        "session_id": "not-a-uuid",
    })
    assert row.session_id is None


# ---------------------------------------------------------------------------
# AdDropRow
# ---------------------------------------------------------------------------


def test_ad_drop_row_from_dict():
    sid = str(uuid4())
    drop = AdDropRow.from_dict({
        "video_id": "adABC",
        "session_id": sid,
        "reasons": ["dom:ytd-ad-slot-renderer", "duration:over_60s"],
    })
    assert drop is not None
    assert drop.video_id == "adABC"
    assert str(drop.session_id) == sid
    assert drop.reasons == ["dom:ytd-ad-slot-renderer", "duration:over_60s"]
    # URL is synthesized when absent
    assert drop.url.endswith("/shorts/adABC")


def test_ad_drop_row_requires_video_id():
    assert AdDropRow.from_dict({"reasons": ["x"]}) is None


# ---------------------------------------------------------------------------
# SessionRow
# ---------------------------------------------------------------------------


def test_session_row_from_dict_roundtrip():
    sid = str(uuid4())
    row = SessionRow.from_dict({
        "session_id": sid,
        "created_at": "2026-04-18T10:00:00+00:00",
        "closed_at": "2026-04-18T11:05:00+00:00",
        "user_agent": "UA",
        "viewport": "1920x1080",
        "videos_scraped": 412,
        "ads_filtered": 67,
        "unique_channels_seen": 238,
        "channels_distribution": {"ChA": 10, "ChB": 5},
    })
    assert row is not None
    assert str(row.session_id) == sid
    assert row.videos_scraped == 412
    assert row.ads_filtered == 67
    assert row.unique_channels_seen == 238
    assert row.channels_distribution == {"ChA": 10, "ChB": 5}
    assert row.created_at.year == 2026
    assert row.closed_at is not None


def test_session_row_rejects_missing_or_bad_uuid():
    assert SessionRow.from_dict({"created_at": "2026-04-18T00:00:00+00:00"}) is None
    assert SessionRow.from_dict({
        "session_id": "garbage",
        "created_at": "2026-04-18T00:00:00+00:00",
    }) is None


def test_session_row_rejects_bad_timestamp():
    assert SessionRow.from_dict({
        "session_id": str(uuid4()),
        "created_at": "not-a-date",
    }) is None


def test_session_row_handles_open_session():
    """A session mid-rotation may be serialized without a closed_at."""
    row = SessionRow.from_dict({
        "session_id": str(uuid4()),
        "created_at": "2026-04-18T10:00:00+00:00",
        "videos_scraped": 0,
    })
    assert row is not None
    assert row.closed_at is None
