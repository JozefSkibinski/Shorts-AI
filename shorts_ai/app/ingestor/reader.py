"""Parse one batch file into (header, observations, footer)."""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from typing import IO


log = logging.getLogger(__name__)


@dataclass
class ParsedBatch:
    header: dict
    observations: list[dict]
    footer: dict | None

    @property
    def machine_id(self) -> str | None:
        return self.header.get("machine_id")

    @property
    def session_id(self) -> str | None:
        return self.header.get("session_id")


class BatchValidationError(ValueError):
    pass


def parse_batch(stream: IO[bytes]) -> ParsedBatch:
    """Read a gzipped JSONL batch. Validates the header is present and first.

    Missing footers are tolerated — a scraper can crash mid-batch and the
    bytes already flushed are still useful. The header is non-negotiable
    because without ``machine_id`` we can't attribute snapshots.
    """
    with gzip.GzipFile(fileobj=stream, mode="rb") as gz:
        lines = [line for line in gz.read().decode("utf-8").splitlines() if line.strip()]

    if not lines:
        raise BatchValidationError("empty batch")

    try:
        first = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise BatchValidationError(f"first line is not JSON: {exc}") from exc

    if first.get("record_type") != "batch_header":
        raise BatchValidationError(
            f"first record is {first.get('record_type')!r}, expected batch_header"
        )
    if not first.get("machine_id") or not first.get("session_id"):
        raise BatchValidationError("batch_header missing machine_id or session_id")

    observations: list[dict] = []
    footer: dict | None = None

    for idx, line in enumerate(lines[1:], start=2):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning("skipping malformed batch line %d: %s", idx, exc)
            continue
        rtype = rec.get("record_type")
        if rtype == "observation":
            observations.append(rec)
        elif rtype == "batch_footer":
            footer = rec
        elif rtype == "batch_header":
            # duplicate headers are a bug; ignore but log
            log.warning("duplicate batch_header at line %d", idx)
        else:
            log.debug("unknown record_type %r, ignoring", rtype)

    return ParsedBatch(header=first, observations=observations, footer=footer)


def parse_batch_bytes(raw: bytes) -> ParsedBatch:
    """Convenience wrapper for in-memory tests."""
    import io

    return parse_batch(io.BytesIO(raw))
