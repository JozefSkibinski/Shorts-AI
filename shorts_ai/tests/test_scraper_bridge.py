"""Bridge unit tests — parsing + jsonl tailing, no DB required."""

from __future__ import annotations

import json
from pathlib import Path

from app.scraper_bridge import ScraperRow, iter_jsonl


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
