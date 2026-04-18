"""Machine identity is persistent across processes."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from machine_identity import config_hash, current, get_machine_id


def test_machine_id_persists(tmp_path: Path):
    path = tmp_path / "id"
    first = get_machine_id(path)
    UUID(first)  # valid UUID
    second = get_machine_id(path)
    assert first == second


def test_machine_id_writes_if_missing(tmp_path: Path):
    path = tmp_path / "nested" / "id"
    assert not path.exists()
    mid = get_machine_id(path)
    assert path.exists()
    assert path.read_text().strip() == mid


def test_config_hash_is_stable_and_order_insensitive():
    a = config_hash({"a": 1, "b": 2})
    b = config_hash({"b": 2, "a": 1})
    assert a == b
    assert a != config_hash({"a": 2, "b": 2})


def test_current_returns_full_metadata(tmp_path: Path):
    path = tmp_path / "id"
    meta = current(config={"k": "v"}, path=path)
    assert meta.machine_id
    assert meta.hostname
    assert meta.scraper_config_hash
    assert len(meta.scraper_config_hash) == 16
