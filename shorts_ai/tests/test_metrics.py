"""Velocity math — uses fake snapshot objects so no DB needed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.pipeline.metrics import (
    _peak_velocity,
    _window_velocity,
    compute_for_seed,
)


@dataclass
class FakeSnap:
    captured_at: datetime
    view_count: int


def _series(start: datetime, points: list[tuple[float, int]]) -> list[FakeSnap]:
    return [FakeSnap(start + timedelta(hours=h), v) for h, v in points]


def test_window_velocity_24h_simple():
    t0 = datetime(2026, 4, 18, 12, tzinfo=timezone.utc)
    snaps = _series(t0, [(0, 0), (12, 60_000), (24, 120_000)])
    v = _window_velocity(snaps, 24.0)
    assert v == 5_000.0  # 120k views over 24h


def test_window_velocity_clips_to_window():
    t0 = datetime(2026, 4, 18, tzinfo=timezone.utc)
    snaps = _series(t0, [(0, 0), (12, 60_000), (40, 300_000)])
    # 24h window: only first two snaps qualify -> 60k / 12h = 5000
    assert _window_velocity(snaps, 24.0) == 5_000.0


def test_window_velocity_returns_none_for_short_series():
    assert _window_velocity([], 24.0) is None
    t0 = datetime(2026, 4, 18, tzinfo=timezone.utc)
    assert _window_velocity(_series(t0, [(0, 100)]), 24.0) is None


def test_peak_velocity_picks_highest_segment():
    t0 = datetime(2026, 4, 18, tzinfo=timezone.utc)
    snaps = _series(
        t0,
        [(0, 0), (1, 1_000), (2, 2_000), (3, 12_000), (4, 12_500)],
    )
    # Best segment is hr 2->3 (10k views in 1h).
    assert _peak_velocity(snaps) == 10_000.0


def test_peak_velocity_ignores_decreases():
    t0 = datetime(2026, 4, 18, tzinfo=timezone.utc)
    # All segments are decreases -> no positive rate -> None
    snaps = _series(t0, [(0, 100), (1, 90), (2, 80)])
    assert _peak_velocity(snaps) is None


def test_compute_for_seed_basic():
    # Fresh (24h-old) video: still inside the 24h window, so both v24 and v72
    # should be populated with the running rate.
    v = compute_for_seed(view_count=120_000, hours_since_publish=24.0)
    assert v.peak == 5_000.0
    assert v.v24 == 5_000.0
    assert v.v72 == 5_000.0
    # 48h-old: outside 24h window, still inside 72h window
    v = compute_for_seed(view_count=240_000, hours_since_publish=48.0)
    assert v.v24 is None
    assert v.v72 == 5_000.0
    # 100h-old: outside both windows
    v = compute_for_seed(view_count=500_000, hours_since_publish=100.0)
    assert v.v24 is None
    assert v.v72 is None
    assert v.peak == 5_000.0


def test_compute_for_seed_handles_zero_hours():
    v = compute_for_seed(view_count=100, hours_since_publish=0)
    assert v.v24 is v.v72 is v.peak is None
