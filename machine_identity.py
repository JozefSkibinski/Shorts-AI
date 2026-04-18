"""Persistent machine identity for the distributed scraper.

Survives restarts. One UUID per physical machine, stored under
``$XDG_STATE_HOME/shorts-scraper/machine_id`` (or ``~/.shorts_scraper_id`` if
XDG is not set). Exposed as a pure function so tests can override the
storage path.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


_DEFAULT_PATH = Path(
    os.environ.get("SHORTS_MACHINE_ID_FILE")
    or os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
) / "shorts-scraper" / "machine_id"
_LEGACY_PATH = Path.home() / ".shorts_scraper_id"


def _read_first(paths: list[Path]) -> str | None:
    for p in paths:
        try:
            text = p.read_text().strip()
            if text:
                return text
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return None


def get_machine_id(path: Path = _DEFAULT_PATH) -> str:
    """Return the UUID for this machine, generating one on first call."""
    existing = _read_first([path, _LEGACY_PATH])
    if existing:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    mid = str(uuid.uuid4())
    path.write_text(mid)
    return mid


def get_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def _git_sha() -> str:
    """Best-effort short git SHA of this repo. Falls back to 'unknown'."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"],
            stderr=subprocess.DEVNULL,
            cwd=Path(__file__).resolve().parent,
            timeout=2,
        )
        return out.decode().strip() or "unknown"
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class MachineMetadata:
    machine_id: str
    hostname: str
    scraper_version: str
    scraper_config_hash: str


def config_hash(config: dict) -> str:
    """Stable short hash of a dict — used to detect config drift across the fleet."""
    import json

    payload = json.dumps(config, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def current(config: dict | None = None, path: Path = _DEFAULT_PATH) -> MachineMetadata:
    return MachineMetadata(
        machine_id=get_machine_id(path),
        hostname=get_hostname(),
        scraper_version=_git_sha(),
        scraper_config_hash=config_hash(config or {}),
    )
