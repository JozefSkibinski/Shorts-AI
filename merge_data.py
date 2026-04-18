"""Merge multiple scraper output sources into a single deduped JSONL.

Accepts any mix of:

  - ``shorts.jsonl``   (the flat per-run file)
  - ``shorts.jsonl`` from other machines / earlier runs
  - batch files under ``batches/**/*.jsonl.gz`` (distributed format;
    observation records get flattened back into the flat shape)

Dedup is by ``video_id``. When the same video appears multiple times,
the record with the **highest view_count** wins — so re-running stats
over the merged file reflects the latest view numbers each machine saw.

Usage:
  python merge_data.py output/shorts.jsonl other-machine/shorts.jsonl
  python merge_data.py output/                         # auto-discover
  python merge_data.py output/ -o merged.jsonl         # custom output
  python merge_data.py output/ --regen-stats           # also rewrite stats.txt
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
from pathlib import Path
from typing import Iterable, Iterator


log = logging.getLogger("merge_data")


OBS_KEYS = {
    "source_video_id": "video_id",
    "url": "url",
    "author_handle": "channel",
    "title": "title",
    "description": "description",
    "hashtags": "hashtags",
    "keywords": "keywords",
    "duration_seconds": "duration_seconds",
    "view_count": "view_count",
    "has_paid_promotion_disclosure": "has_paid_promotion_disclosure",
    "transcript": "transcript",
    "hook": "hook",
    "metadata_source": "metadata_source",
    "transcript_source": "transcript_source",
}


def _iter_flat_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _iter_batch_observations(path: Path) -> Iterator[dict]:
    """Yield observation records from one gzipped batch file, re-keyed to flat shape."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("record_type") != "observation":
                    continue
                flat = {}
                for batch_key, flat_key in OBS_KEYS.items():
                    if batch_key in rec:
                        flat[flat_key] = rec[batch_key]
                if flat.get("video_id"):
                    yield flat
    except OSError as exc:
        log.warning("skipping unreadable batch %s: %s", path, exc)


def discover_inputs(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for p in paths:
        if p.is_file():
            found.append(p)
            continue
        if not p.is_dir():
            log.warning("skipping %s: not a file or directory", p)
            continue
        # Any shorts.jsonl anywhere under this directory
        found.extend(sorted(p.rglob("shorts.jsonl")))
        # All gzipped batch files
        found.extend(sorted(p.rglob("*.jsonl.gz")))
    # Deduplicate by resolved path, preserving order
    seen: set[Path] = set()
    out: list[Path] = []
    for f in found:
        r = f.resolve()
        if r not in seen:
            seen.add(r)
            out.append(f)
    return out


def iter_records(path: Path) -> Iterator[dict]:
    if path.suffix == ".gz":
        yield from _iter_batch_observations(path)
    else:
        yield from _iter_flat_jsonl(path)


def merge(inputs: Iterable[Path]) -> tuple[dict[str, dict], int]:
    """Return (merged_by_id, total_seen)."""
    merged: dict[str, dict] = {}
    seen = 0
    for path in inputs:
        for rec in iter_records(path):
            vid = rec.get("video_id")
            if not vid:
                continue
            seen += 1
            existing = merged.get(vid)
            if existing is None:
                merged[vid] = rec
                continue
            # Keep the record with the highest view count; fall back to the
            # one with more fields populated so we don't lose transcripts.
            new_views = int(rec.get("view_count") or 0)
            old_views = int(existing.get("view_count") or 0)
            if new_views > old_views:
                winner = rec
            elif new_views < old_views:
                winner = existing
            else:
                winner = rec if sum(1 for v in rec.values() if v) > sum(
                    1 for v in existing.values() if v
                ) else existing
            # Merge transcript/hook from the loser so we keep richer text.
            for k in ("transcript", "hook", "description", "keywords"):
                if not winner.get(k):
                    loser = existing if winner is rec else rec
                    if loser.get(k):
                        winner[k] = loser[k]
            merged[vid] = winner
    return merged, seen


def write_output(path: Path, records: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Merge scraper outputs into one jsonl.")
    p.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Files or directories. Directories are scanned for shorts.jsonl and *.jsonl.gz.",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output/merged.jsonl"),
        help="Destination flat JSONL. Default: output/merged.jsonl.",
    )
    p.add_argument(
        "--regen-stats",
        action="store_true",
        help="After merging, run stats.build_report on the merged file "
        "and write to <output>.stats.txt.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    inputs = discover_inputs(args.inputs)
    if not inputs:
        log.error("no inputs found under %s", args.inputs)
        return 1
    log.info("merging %d input files", len(inputs))
    for p in inputs:
        log.debug("  %s", p)

    merged, seen = merge(inputs)
    n = write_output(args.output, merged.values())
    log.info(
        "wrote %d unique videos (from %d raw records) -> %s",
        n,
        seen,
        args.output,
    )

    if args.regen_stats:
        try:
            import stats as stats_module
        except ImportError:
            log.warning("stats module not importable; skipping --regen-stats")
            return 0
        stats_path = args.output.with_suffix(".stats.txt")
        count = stats_module.write_report(args.output, stats_path)
        log.info("wrote stats for %d records -> %s", count, stats_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
