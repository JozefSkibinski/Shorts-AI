"""Batch reader: parsing, envelope validation, malformed tolerance."""

from __future__ import annotations

import gzip
import io
import json

import pytest

from app.ingestor.reader import BatchValidationError, parse_batch_bytes


def _gz(records: list[dict]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(("\n".join(json.dumps(r) for r in records) + "\n").encode())
    return buf.getvalue()


def _header(session_id="s1", machine_id="m1") -> dict:
    return {
        "record_type": "batch_header",
        "machine_id": machine_id,
        "session_id": session_id,
        "hostname": "host",
        "scraper_version": "abc123",
    }


def _obs(vid="a") -> dict:
    return {"record_type": "observation", "source_video_id": vid, "view_count": 100}


def _footer() -> dict:
    return {"record_type": "batch_footer", "records": 1}


def test_happy_path():
    raw = _gz([_header(), _obs("a"), _obs("b"), _footer()])
    parsed = parse_batch_bytes(raw)
    assert parsed.machine_id == "m1"
    assert parsed.session_id == "s1"
    assert len(parsed.observations) == 2
    assert [o["source_video_id"] for o in parsed.observations] == ["a", "b"]
    assert parsed.footer is not None


def test_missing_footer_is_tolerated():
    raw = _gz([_header(), _obs("a"), _obs("b")])
    parsed = parse_batch_bytes(raw)
    assert len(parsed.observations) == 2
    assert parsed.footer is None


def test_empty_batch_is_error():
    raw = _gz([])
    with pytest.raises(BatchValidationError):
        parse_batch_bytes(raw)


def test_missing_header_is_quarantined():
    raw = _gz([_obs("a"), _footer()])
    with pytest.raises(BatchValidationError):
        parse_batch_bytes(raw)


def test_header_without_machine_id_rejected():
    bad = {"record_type": "batch_header", "session_id": "s1"}
    raw = _gz([bad, _obs("a")])
    with pytest.raises(BatchValidationError):
        parse_batch_bytes(raw)


def test_bad_json_line_is_skipped_not_fatal():
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write((json.dumps(_header()) + "\n").encode())
        gz.write(b"this is not json\n")
        gz.write((json.dumps(_obs("a")) + "\n").encode())
        gz.write((json.dumps(_footer()) + "\n").encode())
    parsed = parse_batch_bytes(buf.getvalue())
    assert [o["source_video_id"] for o in parsed.observations] == ["a"]
