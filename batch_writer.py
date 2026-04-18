"""Hourly batched JSONL.gz writer for the distributed scraper.

Each batch is one file per (machine, session, hour) with the layout from
the distributed spec:

  header       (one line)
  observation  (0..N lines — only non-ad videos)
  footer       (one line on close)

Files land under::

    <output_dir>/batches/<YYYY>/<MM>/<DD>/<machine_id>/<session_id>-<hour_epoch>.jsonl.gz

``output_dir`` is typically ``output/`` on local scraper machines. A separate
uploader (S3, R2, rsync...) then mirrors finished files into shared storage.
Upload is a swappable callback so we don't couple the scraper to any cloud
SDK here.

A rotation boundary is either:
  - crossing into a new UTC hour
  - exceeding ``records_per_file`` observations (default 500)

Each completed batch is gzipped on close and optionally forwarded to
``on_close(path)`` so a caller can upload + delete.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


log = logging.getLogger(__name__)


OnCloseFn = Callable[[Path], None]


@dataclass
class BatchEnvelope:
    machine_id: str
    hostname: str
    scraper_version: str
    scraper_config_hash: str
    session_id: str
    session_started_at: str
    user_agent: str
    viewport: str

    def header(self) -> dict:
        return {
            "record_type": "batch_header",
            "machine_id": self.machine_id,
            "hostname": self.hostname,
            "scraper_version": self.scraper_version,
            "scraper_config_hash": self.scraper_config_hash,
            "session_id": self.session_id,
            "session_started_at": self.session_started_at,
            "user_agent": self.user_agent,
            "viewport": self.viewport,
        }


class BatchWriter:
    """One BatchWriter per scraper session. Rotates internally by hour."""

    def __init__(
        self,
        output_dir: Path,
        envelope: BatchEnvelope,
        *,
        records_per_file: int = 500,
        on_close: OnCloseFn | None = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.root = Path(output_dir) / "batches"
        self.envelope = envelope
        self.records_per_file = max(1, records_per_file)
        self._on_close = on_close
        self._now = now_fn

        self._buffer: io.StringIO | None = None
        self._current_path: Path | None = None
        self._current_hour: int | None = None
        self._records_in_file = 0
        self._closed = False

    # ----- public API -------------------------------------------------------

    def write_observation(self, obs: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("BatchWriter.write_observation after close()")
        self._ensure_current_file()
        obs_out = dict(obs)
        obs_out["record_type"] = "observation"
        obs_out.setdefault("observed_at", self._now().isoformat())
        self._buffer.write(json.dumps(obs_out, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
        self._records_in_file += 1
        if self._records_in_file >= self.records_per_file:
            self._rotate()

    def close(self, *, footer_extras: dict[str, Any] | None = None) -> Path | None:
        if self._closed:
            return self._current_path
        self._closed = True
        return self._finalize_current(footer_extras=footer_extras)

    # ----- internals --------------------------------------------------------

    def _ensure_current_file(self) -> None:
        now = self._now()
        hour = int(now.replace(minute=0, second=0, microsecond=0).timestamp())

        if self._buffer is None:
            self._open_new_file(now, hour)
            return

        if hour != self._current_hour:
            self._rotate()
            # open a new one at the new hour
            now = self._now()
            hour = int(now.replace(minute=0, second=0, microsecond=0).timestamp())
            self._open_new_file(now, hour)

    def _open_new_file(self, now: datetime, hour_epoch: int) -> None:
        subdir = (
            self.root
            / f"{now.year:04d}"
            / f"{now.month:02d}"
            / f"{now.day:02d}"
            / self.envelope.machine_id
        )
        subdir.mkdir(parents=True, exist_ok=True)
        filename = f"{self.envelope.session_id}-{hour_epoch}.jsonl.gz"
        self._current_path = subdir / filename
        self._current_hour = hour_epoch
        self._records_in_file = 0

        # Buffer in memory until rotation — then gzip the whole thing.
        self._buffer = io.StringIO()
        self._buffer.write(json.dumps(self.envelope.header(), ensure_ascii=False) + "\n")
        log.info("opened batch %s", self._current_path.name)

    def _rotate(self) -> Path | None:
        return self._finalize_current(footer_extras=None)

    def _finalize_current(
        self, *, footer_extras: dict[str, Any] | None
    ) -> Path | None:
        if self._buffer is None or self._current_path is None:
            return None

        footer = {
            "record_type": "batch_footer",
            "machine_id": self.envelope.machine_id,
            "session_id": self.envelope.session_id,
            "records": self._records_in_file,
            "closed_at": self._now().isoformat(),
        }
        if footer_extras:
            footer.update(footer_extras)
        self._buffer.write(json.dumps(footer, ensure_ascii=False) + "\n")

        path = self._current_path
        self._compress_to(path, self._buffer.getvalue())

        self._buffer = None
        self._current_path = None
        self._current_hour = None
        self._records_in_file = 0

        if self._on_close:
            try:
                self._on_close(path)
            except Exception as exc:
                log.warning("on_close(%s) failed: %s", path, exc)
        return path

    @staticmethod
    def _compress_to(dest: Path, payload: str) -> None:
        """Atomic gzip write via tmp + rename."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=dest.name + ".", dir=str(dest.parent), suffix=".tmp"
        )
        tmp_path = Path(tmp)
        try:
            with open(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as gz:
                gz.write(payload.encode("utf-8"))
            shutil.move(str(tmp_path), dest)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
