"""metrics_merge: 5-minute collapse window without touching Postgres."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.ingestor import metrics_merge
from app.ingestor.metrics_merge import _should_collapse


@dataclass
class FakeSnap:
    view_count: int | None
    captured_at: datetime


def test_collapse_when_recent_same_views():
    recent = FakeSnap(view_count=1_000_000, captured_at=datetime.now(timezone.utc))
    # new observation 50 views higher -> collapse
    assert _should_collapse(recent, 1_000_050) is True


def test_no_collapse_when_numbers_moved():
    recent = FakeSnap(view_count=1_000_000, captured_at=datetime.now(timezone.utc))
    assert _should_collapse(recent, 1_002_000) is False


def test_no_collapse_without_recent():
    assert _should_collapse(None, 100) is False


class FakeScalarOrNone:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v


class FakeSession:
    """Minimal Session stand-in. Tracks .add() calls + returns a recent snap."""

    def __init__(self, recent=None):
        self.added: list = []
        self._recent = recent

    def execute(self, stmt):
        return FakeScalarOrNone(self._recent)

    def add(self, obj):
        self.added.append(obj)


def test_append_snapshot_inserts_new():
    now = datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc)
    obs = {"observed_at": now.isoformat(), "view_count": 500_000}
    db = FakeSession()
    inserted = metrics_merge.append_snapshot(
        db, video_id=1, obs=obs, machine_id=str(uuid4()), session_id=str(uuid4())
    )
    assert inserted is True
    assert len(db.added) == 1
    snap = db.added[0]
    assert snap.view_count == 500_000


def test_append_snapshot_collapses_same_machine_within_window():
    now = datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc)
    recent = FakeSnap(view_count=500_000, captured_at=now - timedelta(minutes=2))
    obs = {"observed_at": now.isoformat(), "view_count": 500_020}
    db = FakeSession(recent=recent)
    inserted = metrics_merge.append_snapshot(
        db, video_id=1, obs=obs, machine_id=str(uuid4()), session_id=str(uuid4())
    )
    assert inserted is False
    assert db.added == []


def test_append_snapshot_no_machine_id_always_inserts():
    """Without a machine uuid we can't bucket the collapse query — still insert."""
    now = datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc)
    obs = {"observed_at": now.isoformat(), "view_count": 100}
    db = FakeSession(recent=FakeSnap(100, now))  # wouldn't matter; query skipped
    inserted = metrics_merge.append_snapshot(
        db, video_id=1, obs=obs, machine_id=None, session_id=None
    )
    assert inserted is True
    assert len(db.added) == 1
