"""Object-store adapter.

Two concrete backends:

- ``LocalBucket`` — operates on a local filesystem root. Used for dev,
  tests, and single-machine deployments where all scraper output lives
  on the ingestor host.
- ``S3Bucket`` — thin wrapper over ``boto3``/``botocore`` that you wire
  up at deploy time. Intentionally lazy-imported so the rest of the
  ingestor stays usable without any cloud SDKs installed.

Both implement the same ``Bucket`` protocol: list new keys, open one for
reading, move to an archive prefix after a successful ingestion.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterable, Protocol


log = logging.getLogger(__name__)


class Bucket(Protocol):
    """Protocol the ingestor depends on."""

    def list_keys(self, prefix: str = "", suffix: str = ".jsonl.gz") -> Iterable[str]: ...

    def open(self, key: str) -> IO[bytes]: ...

    def move(self, src_key: str, dest_key: str) -> None: ...

    def archive_key_for(self, key: str) -> str: ...


# ---------------------------------------------------------------------------
# Local filesystem bucket
# ---------------------------------------------------------------------------


@dataclass
class LocalBucket:
    root: Path
    archive_prefix: str = "archived"

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        key = key.lstrip("/")
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError(f"key escapes bucket root: {key}")
        return path

    def list_keys(
        self, prefix: str = "", suffix: str = ".jsonl.gz"
    ) -> Iterable[str]:
        base = self._resolve(prefix) if prefix else self.root
        if not base.exists():
            return
        for p in sorted(base.rglob(f"*{suffix}")):
            rel = p.relative_to(self.root).as_posix()
            if rel.startswith(self.archive_prefix + "/"):
                continue
            yield rel

    def open(self, key: str) -> IO[bytes]:
        return self._resolve(key).open("rb")

    def move(self, src_key: str, dest_key: str) -> None:
        src = self._resolve(src_key)
        dest = self._resolve(dest_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), dest)

    def archive_key_for(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self.archive_prefix}/{key}"


# ---------------------------------------------------------------------------
# S3 bucket
# ---------------------------------------------------------------------------


class S3Bucket:
    """Usage::

        bucket = S3Bucket("shorts-raw", prefix="batches/")

    Requires ``boto3`` on PYTHONPATH at runtime. Not imported at module
    load so the ingestor stays usable on machines without AWS SDKs.
    """

    def __init__(
        self,
        bucket_name: str,
        *,
        prefix: str = "",
        archive_prefix: str = "archived",
        client=None,
    ) -> None:
        self.bucket_name = bucket_name
        self.prefix = prefix.rstrip("/")
        self.archive_prefix = archive_prefix.strip("/")
        self._client = client

    @property
    def client(self):
        if self._client is not None:
            return self._client
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "S3Bucket requires boto3. Install with: pip install boto3"
            ) from exc
        self._client = boto3.client("s3")
        return self._client

    def list_keys(
        self, prefix: str = "", suffix: str = ".jsonl.gz"
    ) -> Iterable[str]:
        full = f"{self.prefix}/{prefix}".strip("/")
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=full):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(suffix) and f"/{self.archive_prefix}/" not in key:
                    yield key

    def open(self, key: str):
        resp = self.client.get_object(Bucket=self.bucket_name, Key=key)
        return resp["Body"]

    def move(self, src_key: str, dest_key: str) -> None:
        self.client.copy_object(
            Bucket=self.bucket_name,
            CopySource={"Bucket": self.bucket_name, "Key": src_key},
            Key=dest_key,
        )
        self.client.delete_object(Bucket=self.bucket_name, Key=src_key)

    def archive_key_for(self, key: str) -> str:
        return f"{self.archive_prefix}/{key}"
