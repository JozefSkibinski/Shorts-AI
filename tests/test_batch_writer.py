"""BatchWriter: envelope, rotation, gzip, on_close hook."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from batch_writer import BatchEnvelope, BatchWriter


def _envelope(session_id="s1") -> BatchEnvelope:
    return BatchEnvelope(
        machine_id="aaaa-bbbb",
        hostname="scraper-test",
        scraper_version="deadbee",
        scraper_config_hash="cafecafe",
        session_id=session_id,
        session_started_at="2026-04-18T10:00:00+00:00",
        user_agent="UA",
        viewport="1920x1080",
    )


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


def _read_gz(path: Path) -> list[dict]:
    with gzip.open(path, "rb") as f:
        return [json.loads(line) for line in f.read().decode().splitlines() if line.strip()]


def test_writes_header_observations_and_footer(tmp_path: Path):
    clock = FakeClock(datetime(2026, 4, 18, 10, 5, 0, tzinfo=timezone.utc))
    w = BatchWriter(tmp_path, _envelope(), now_fn=clock)
    w.write_observation({"source_video_id": "a", "view_count": 100})
    w.write_observation({"source_video_id": "b", "view_count": 200})
    path = w.close(footer_extras={"ads_filtered": 3})
    assert path is not None
    assert path.exists()
    records = _read_gz(path)
    assert records[0]["record_type"] == "batch_header"
    assert records[0]["machine_id"] == "aaaa-bbbb"
    assert records[0]["session_id"] == "s1"
    assert [r["record_type"] for r in records[1:-1]] == ["observation", "observation"]
    assert records[-1]["record_type"] == "batch_footer"
    assert records[-1]["records"] == 2
    assert records[-1]["ads_filtered"] == 3


def test_path_layout_uses_spec_partition(tmp_path: Path):
    clock = FakeClock(datetime(2026, 4, 18, 14, 2, 0, tzinfo=timezone.utc))
    w = BatchWriter(tmp_path, _envelope("sess-xyz"), now_fn=clock)
    w.write_observation({"source_video_id": "a"})
    path = w.close()
    # batches/2026/04/18/<machine>/<session>-<hour_epoch>.jsonl.gz
    rel = path.relative_to(tmp_path).as_posix()
    parts = rel.split("/")
    assert parts[0] == "batches"
    assert parts[1:4] == ["2026", "04", "18"]
    assert parts[4] == "aaaa-bbbb"
    assert parts[5].startswith("sess-xyz-") and parts[5].endswith(".jsonl.gz")


def test_rotates_at_hour_boundary(tmp_path: Path):
    clock = FakeClock(datetime(2026, 4, 18, 10, 55, 0, tzinfo=timezone.utc))
    closed: list[Path] = []
    w = BatchWriter(tmp_path, _envelope(), now_fn=clock, on_close=closed.append)
    w.write_observation({"source_video_id": "a"})
    clock.advance(hours=1)  # 11:55 — new hour bucket
    w.write_observation({"source_video_id": "b"})
    w.close()
    assert len(closed) == 2, closed
    # File 1 contains only 'a', file 2 only 'b'.
    recs1 = _read_gz(closed[0])
    recs2 = _read_gz(closed[1])
    assert any(r.get("source_video_id") == "a" for r in recs1)
    assert not any(r.get("source_video_id") == "a" for r in recs2)
    assert any(r.get("source_video_id") == "b" for r in recs2)


def test_rotates_on_records_limit(tmp_path: Path):
    clock = FakeClock(datetime(2026, 4, 18, 10, 5, 0, tzinfo=timezone.utc))
    closed: list[Path] = []
    w = BatchWriter(
        tmp_path, _envelope(), now_fn=clock, on_close=closed.append, records_per_file=2
    )
    for i in range(5):
        w.write_observation({"source_video_id": f"v{i}"})
    w.close()
    # 5 records with limit 2 -> rotations after 2 and 4 -> 3 files total
    assert len(closed) == 3


def test_close_after_close_is_noop(tmp_path: Path):
    w = BatchWriter(tmp_path, _envelope())
    w.write_observation({"source_video_id": "a"})
    w.close()
    w.close()  # no crash


def test_observation_after_close_raises(tmp_path: Path):
    w = BatchWriter(tmp_path, _envelope())
    w.close()
    with pytest.raises(RuntimeError):
        w.write_observation({"source_video_id": "a"})


def test_observed_at_defaults_to_now(tmp_path: Path):
    clock = FakeClock(datetime(2026, 4, 18, 10, 5, 0, tzinfo=timezone.utc))
    w = BatchWriter(tmp_path, _envelope(), now_fn=clock)
    w.write_observation({"source_video_id": "a"})
    path = w.close()
    recs = _read_gz(path)
    obs = next(r for r in recs if r["record_type"] == "observation")
    assert obs["observed_at"].startswith("2026-04-18T10:05")
