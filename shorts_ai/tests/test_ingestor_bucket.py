"""LocalBucket adapter: listing, opening, archiving."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from app.ingestor.bucket import LocalBucket


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _gz(text: str = "hi") -> bytes:
    import io

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(text.encode())
    return buf.getvalue()


def test_list_keys_finds_jsonl_gz_files(tmp_path: Path):
    bucket = LocalBucket(tmp_path)
    _write(tmp_path / "2026/04/18/m1/a.jsonl.gz", _gz())
    _write(tmp_path / "2026/04/18/m2/b.jsonl.gz", _gz())
    _write(tmp_path / "2026/04/18/m2/notice.txt", b"skip me")
    keys = sorted(bucket.list_keys())
    assert keys == [
        "2026/04/18/m1/a.jsonl.gz",
        "2026/04/18/m2/b.jsonl.gz",
    ]


def test_list_keys_ignores_archived(tmp_path: Path):
    bucket = LocalBucket(tmp_path)
    _write(tmp_path / "2026/a.jsonl.gz", _gz())
    _write(tmp_path / "archived/2026/b.jsonl.gz", _gz())
    keys = sorted(bucket.list_keys())
    assert keys == ["2026/a.jsonl.gz"]


def test_open_returns_readable(tmp_path: Path):
    bucket = LocalBucket(tmp_path)
    _write(tmp_path / "a.jsonl.gz", _gz("payload"))
    with bucket.open("a.jsonl.gz") as f:
        raw = f.read()
    # gzipped bytes
    with gzip.open(tmp_path / "a.jsonl.gz", "rb") as g:
        expected = g.read()
    import gzip as _gzip

    assert _gzip.decompress(raw) == expected


def test_move_archives(tmp_path: Path):
    bucket = LocalBucket(tmp_path)
    src = tmp_path / "2026/a.jsonl.gz"
    _write(src, _gz())
    bucket.move("2026/a.jsonl.gz", bucket.archive_key_for("2026/a.jsonl.gz"))
    assert not src.exists()
    assert (tmp_path / "archived/2026/a.jsonl.gz").exists()


def test_key_escape_is_blocked(tmp_path: Path):
    bucket = LocalBucket(tmp_path)
    with pytest.raises(ValueError):
        bucket.open("../etc/passwd")
